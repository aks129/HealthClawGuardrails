candidate: remove identifier values from redaction instead of keeping the last four characters (#112)

## What

`r6.redaction.apply_redaction` (the Safe Harbor / standard profile) kept the
last four characters of every `identifier.value` (`'***' + val[-4:]`). It now
removes `value` entirely and keeps `system` and `type`, so a reader can still
see that a patient had an SSN or an MRN of some kind, but not any part of the
value.

`apply_patient_controlled_redaction` had the same shape one layer down: its
docstring said the injected `https://healthclaw.io/patient-id` identifier is
the *sole* identifier on the output, but the code filtered by a keyword
denylist (`'mrn', 'facility', 'org/', 'example.org'`, plus two hardcoded SSN
systems) and let anything that didn't match through with its value intact —
a MRN under `https://fhir.example-health.test/ids` survived untouched. That
function is now built, not filtered: the output identifier list is always
exactly the one injected entry, matching what the docstring always claimed.

Touched:
- `r6/redaction.py` — both functions, plus the module docstring and the
  `apply_patient_controlled_redaction` docstring.
- `SECURITY.md`, `README.md` (two lines), `skills/phi-redaction/SKILL.md`,
  `skills/fhir-r6-guardrails/SKILL.md` — the "masked to last 4 characters"
  claim, corrected everywhere it appeared on a public surface.
- `tests/test_redaction_identifiers.py` (new) — the direct guard.
- `tests/test_terminology_labels.py` — added an ordering pin for the
  strip-then-label sequence (see Architecture review).
- Seven existing tests that pinned `'***'` fixed to assert the value is gone:
  `test_r6_routes.py`, `test_fhir_proxy.py` (x2), `test_medplum_in_front.py`,
  `test_context_builder.py`, `test_r6_dashboard.py` (x2),
  `test_public_fhir_servers.py`.
- `tests/test_deidentification_language.py` — the guard that forbids an
  unhedged "Safe Harbor" claim on a public surface. Its own explanatory text
  named the last-four truncation as the reason the claim was wrong; updated
  to say identifier values are now removed, and that the profile remains a
  compensating control (birth year, state, and country still pass through)
  so the disclaimer requirement itself does not change.

Not touched: telecom (`[Redacted]`), address line/city/postalCode (removed,
state/country kept), birthDate (year only), names (initials) — none of those
were the finding, and the ruling text asked specifically about identifiers.

## Why (patient problem + property protected)

The patient problem: a redacted record that still carries `***-4567` is not
de-identified for someone who already has a partial SSN, an insurance card,
or a related record — the classic mosaic/linkage attack Safe Harbor's
"remove, don't truncate" rule exists to close. `docs/2026-08-16-hard-truths.md`
§4 named this exact gap as the worst-scored artifact in the codebase: the
most polished document (SECURITY.md) made the least true claim.

The property protected: **an identifier value that could re-identify a
patient does not leave the redaction boundary, in either redaction profile.**
Before this change that property was false for both `apply_redaction` and
`apply_patient_controlled_redaction`.

## Decision: code was wrong, not the policy document

The brief asked me to decide explicitly which side was correct — SECURITY.md
said "HIPAA Safe Harbor" in spirit and the code truncated; those are opposite
claims and only one can be right.

**The policy document is right. The code was wrong, and I fixed the code.**

HIPAA Safe Harbor §164.514(b)(2)(i) lists, among the 18 identifiers to be
removed: (G) Social Security numbers, (H) medical record numbers, (I) health
plan beneficiary numbers, (J) account numbers. The rule's verb is "removed,"
full stop — there is no "except the last four characters" carve-out anywhere
in the enumerated list, and a truncated SSN or MRN is a standard
re-identification vector precisely because so few individuals share a given
last-four suffix within a small population (the same reasoning the rule
applies to dates and ZIP codes elsewhere in the same section). Keeping
`system`/`type` is not itself an identifier value under (G)–(J): it says
"this person had an SSN on file," not what the SSN was, and it is not on the
enumerated list.

I did not soften SECURITY.md's disclaimer language to match a truncating
implementation, because there was no reading of §164.514(b)(2)(i) under which
truncation satisfies it. The disclaimer itself (Safe-Harbor-*style*, not a
legal determination) stays, because the profile still isn't Expert
Determination and still keeps birth year / state / country — that hedge was
never about the identifier truncation specifically, and removing the
identifier value doesn't retire it.

## Architecture review

**Serves the vision or adjacent?** Directly serves it. The constitution says
documentation is product surface and a doc that contradicts the system is a
defect with the same severity as the code being wrong — this was exactly
that defect, in the one document (SECURITY.md) a partner's compliance
reviewer reads first.

**Honest failure mode and who notices?** The remaining failure mode is
unchanged and disclosed: this is field-level redaction, not Expert
Determination or a certified statistical/expert method, and free text
outside the fields this profile touches (a narrative, an extension, a
clinician's note field this profile doesn't know about) can still carry PHI
if it isn't one of the fields `_redact_recursive` walks. Nobody but a future
code reader notices that gap today; it's the same limit `#112` already
tracks and this PR does not change its scope. What *did* change: identifiers
specifically are now removed rather than partially retained, closing the
specific mosaic-attack vector the hard-truths audit named.

**What does it make harder later?** Nothing structural. If a future feature
needs to correlate the SAME patient's redacted records across two reads
(e.g. to show "you have 3 records with an MRN on file" without re-identifying
which), `system`/`type` survive and support that; if it needs a *stable but
non-reversible* identifier token, that's a new, deliberate feature (a hash or
a per-tenant pseudonym), not a reason to keep truncated values around as a
found-money substitute. `apply_patient_controlled_redaction`'s canonical
`healthclaw.io/patient-id` identifier already is that stable token for the
patient's own copy of their data.

**How is it proven, with what data, run by whom?** By this session, against
synthetic fixtures only (`test_redaction_identifiers.py`, and the existing
suite's synthetic SSNs/MRNs like `000-00-9999`, `MRN-99`). Mutation-checked
directly (see below) rather than only asserted. Not proven against a live
upstream feed's actual identifier shapes — that's the same limit every other
redaction test in this suite already carries.

## Ran

```
$ uv run python -m pytest tests/test_redaction_identifiers.py -q
5 passed in 0.09s

$ uv run python -m pytest tests/test_ratchets.py tests/test_guardrail_conformance.py tests/test_redaction*.py tests/test_terminology_labels.py -q
83 passed in 2.14s

$ uv run python -m pytest tests/ -q
3163 passed, 13 skipped, 1 xfailed, 2 warnings in 82.65s (0:01:22)

$ uv run ruff check .
All checks passed!
```

Grade A confirmed: `tests/test_guardrail_conformance.py::test_our_deployment_records_full_conformance`
asserts `report.score == (7, 7)` and `report.grade == "A"`, and it passed.
The probe that checks identifiers is `probe_phi_redaction` in
`r6/conformance/probes.py:600`, specifically the checks named
`"SSN-class identifier masked"` (line 624, direct read) and
`"SSN-class identifier masked (search)"` (line 649, search path) — both
assert the literal synthetic SSN (`_SSN = "000-00-9999"`) is absent from the
response body. Removing the value outright still satisfies an absence check;
nothing about this change weakens what the probe verifies, and the change
makes the check strictly harder to pass by accident (a stub with an empty
body still passes it, same as before — see the probe's own two-sided-check
caveat in its comments, unrelated to this PR).

## Mutation evidence

Three guards, each broken in place, confirmed red, then restored and
confirmed green. Observed output only, all in this worktree:

**1. Standard-profile identifier removal** — restored
`ident['value'] = '***' + val[-4:] if len(val) > 4 else '***'` in
`_redact_fields`:
```
FAILED tests/test_redaction_identifiers.py::test_standard_redaction_removes_every_identifier_value
FAILED tests/test_redaction_identifiers.py::test_standard_redaction_reaches_dict_shaped_and_nested_identifiers
2 failed, 3 passed in 0.09s
```
Restored the fix, re-ran: `5 passed in 0.07s`.

**2. Patient-controlled sole-identifier guarantee** — restored the old
keyword-denylist block in `apply_patient_controlled_redaction`:
```
FAILED tests/test_redaction_identifiers.py::test_patient_controlled_leaves_only_the_healthclaw_identifier
AssertionError: ... Left contains one more item: {'system':
'https://fhir.example-health.test/ids', ..., 'value': 'MRN-7749-XYZ'}
1 failed, 4 passed in 0.08s
```
Restored the fix, re-ran: `5 passed in 0.06s`.

**3. Strip-before-label ordering** (the non-negotiable flagged for this PR:
`label_codings` must run on the already-redacted tree, not the raw upstream
one) — swapped the two calls in `apply_redaction` so `label_codings` ran
first:
```
FAILED tests/test_terminology_labels.py::test_label_codings_runs_after_the_redaction_strip
AssertionError: label_codings must run after the strip removed the upstream
display; it instead saw ['Glucose for Jane Secret']
1 failed in 0.07s
```
Restored the original order (`_redact_recursive(redacted)` then
`label_codings(redacted)`), re-ran: `5 passed in 0.06s`. I did not move this
ordering — it was already correct — and added the test because none of the
existing tests pinned the *order* directly (they pinned the outcome, which a
swap could still pass by coincidence on a fixture with only one coding).
Full suite re-run after all three restores: `3163 passed, 13 skipped,
1 xfailed` — identical counts to the pre-mutation run, confirming the
restores were exact.

## What was NOT run

- `npx tsc --noEmit` / `npm test` in `services/agent-orchestrator` — no
  TypeScript was touched.
- Playwright e2e — out of scope per `docs/agent-task-guide.md` §6 (currently
  red on `main` for environment reasons and gives no signal either way).
- No live upstream FHIR server (Aidbox/HAPI/Medplum) run — all evidence is
  against the local SQLite store and synthetic fixtures, consistent with
  every other redaction test in this suite.
- Did not run the Postgres CI lane locally (no column width changed, so nothing
  in this diff is column-length-sensitive, but I have not independently
  confirmed the lane green).

## Generalization check

The fix is `ident.pop('value', None)` in one loop (standard profile) and a
constructed one-entry list (patient-controlled profile) — not a fix that
special-cases the SSN system or any particular shape. It applies uniformly to
every identifier on every resource type that flows through either function,
including the dict-shaped `Reference.identifier` and nested
`Observation.identifier` cases the new test exercises specifically because
the old code's `isinstance(identifiers, list)` guard made it easy to assume
identifiers only ever appear as `resource.identifier` list entries. It does
not depend on English-language system-name keywords (the exact class of bug
being removed from the patient-controlled path) and needs no future
maintenance as new identifier systems appear in upstream feeds.

## Was truncated identifier output already committed to the repo?

Checked, not assumed. Grepped every tracked file (not just code) for the old
`***XXXX` shape. Four hits, all synthetic:

- `docs/evidence/2026-08-16-set1-guardrail-core.md` and
  `docs/evidence/2026-08-16-set2-connectors.md` — dated run-log transcripts,
  each explicitly labeled synthetic in its own text (`urn:set2:evidence` as
  the identifier system on one; "returned record (whole body, synthetic)" on
  the other). Left untouched as history and annotated in place (this PR) with
  a dated note that redaction behaviour changed 2026-09-04 — a reader six
  months out should not mistake `***6789` for current behaviour.
- `examples/aidbox-healthclaw-guardrails/README.md` — a living usage example
  on synthetic patient `pt-demo`, not a dated artifact. Fixed in this PR (it
  would otherwise have silently contradicted the code the moment this merged).
- `skills/phi-redaction/SKILL.md` — prose description, already corrected
  above.

**No real credential or patient data was ever committed to this repository.**
That is a checked claim (the grep above), not an inference from the absence
of alarm.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
