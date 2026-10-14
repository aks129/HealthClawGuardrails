# Synthetic Hospital cohort shakeout, 2026-10-01

## Verdict

**The guardrails PASS on realistic data. Stable beta is NOT reached yet**, for
two reasons outside this change: production CareAgents runs a build from
before the calm hub, and records whose source sends no codes are unreadable
to the assistant. Details and owners are in "Findings".

This change also builds beta pathway spec section 4.2 (real-record invites
in the database), which was the engineering blocker for stage 1.

## What was run

The cohort is 20 synthetic patients from the Synthetic Hospital v1.3 release
(`patient_profiles.jsonl` and `benchmark_v1.3.db`, MIT). They were picked to
vary: ages 0 to 82, 2 to 8 visits, 14 to 90 FHIR entries each, ICU, ED and
telehealth visits, the most allergies and medications, notes with non-ASCII
text, no labs and the most labs. Each patient carries a canary surname and
phone; the realistic clinician names in the data are canaries too.

The simulator's own code was **not** run (not permitted in this session), so
`tests/test_synthetic_hospital_live.py` and the upstream proxy path stay
unexercised. The same patients went through the paths a person uses instead.

| Tool | What it does |
|---|---|
| `scripts/synthetic_hospital_cohort.py` | Builds the cohort as FHIR R4 bundles from the release files |
| `scripts/cohort_shakeout.py` | Per patient: sign up, consent, upload, chat, labs timeline, approvals, cross-account probes, delete and sign up again |
| `scripts/cohort_engine_probe.py` | Per patient: ingest into HealthClaw, read every type back, scan for canaries and source free text, count audit rows, cross-tenant probes |

Stack: HealthClaw, CareAgents web and worker, on local Postgres 16 with
`READ_AUTH_ENABLED=true` and `TERMINOLOGY_LOOKUP_ENABLED=1` (the production
settings), plus a scripted OpenAI-compatible stand-in model that logs every
request. The stand-in calls tools in a fixed order and echoes their results.
It shows what a real model would be sent. It does not measure answer quality.

## Results

| Gate or check | Result |
|---|---|
| G2 `uv run pytest -q` (baseline, before this change) | 4,652 passed, 1 failed, 53 skipped. The failure is `test_an_unwritable_target_leaves_no_partial_marker`, which assumes a non-root user; this container runs as root |
| G2 `uv run ruff check .` | Passed |
| G5 guardrail conformance, local Postgres stack | Grade A, 7/7 |
| CareAgents journey, 20 patients, Postgres, production settings | 503 passed, 0 failed |
| Engine probe, 20 patients, Postgres, production settings | 640 passed, 0 failed |
| Browser suite, `e2e/` (includes 375 px hub) | 56 passed |
| Phone pass at 375 px on a cohort account: hub, settings, chat | No horizontal scroll on any page |
| G8 `scripts/prod_watch.py --expect-sha d78fcba` | 17 OK, 1 FAIL: CareAgents runs `ebc41dd` |

### Vision checks (QA sign-off standard, section 1)

| # | Result | Evidence |
|---|---|---|
| V1 | PASS | No canary or clinician name in any chat answer, labs timeline, upload response, or in any of the model requests logged across runs. The intake review page shows the person's own name and phone by design (`r6/actions/review.py`); it shows no clinician name |
| V2 | PASS | Every tenant read has audit rows; each engine read added at least one |
| V3 | PASS | An intake form waits on the approvals page; its status is not sent before Approve; a bare submit without the allergy attestation does not send |
| V4 | PASS with notes | Journey completes at 375 px with no horizontal scroll. See finding 5 |
| V5 | Not assessed | Out of scope for this run |
| V6 | PASS | Account B is refused account A's agent, connection, upload, rename, approvals and chat page. Tenant B's token with tenant A's header is refused (401/403) when read auth is on |

### Mutation evidence (gate G3)

The drivers were run against deliberately broken builds (local monkeypatches,
not committed):

- Engine redaction disabled: the engine probe failed 26 checks, 18 of them
  canary leaks (clinician names in DocumentReference).
- Engine redaction disabled alone: the CareAgents journey still passed,
  because the assistant's tools project every record to type, name, status
  and date. That is a second, independent layer.
- Both layers disabled: the journey failed, with clinician names in 32 model
  requests and in a chat answer.
- The new invite tests fail when the app ignores the table (3 fail), when
  revocation is ignored (2 fail), and when the table is read in `off` mode
  (2 fail).

## Findings

Ranked by effect on the stable beta.

**1. Production CareAgents is not on the current build.** It serves
`ebc41dd` (built 2026-09-25), before the calm hub (#840, #843). Testers do
not see the hub the beta gate depends on. Owner action: redeploy per
`RELEASING.md` section 4.

**2. Records the source sends without codes are unreadable to the
assistant.** Measured over the model requests in this run, with the
terminology resolver on:

| What the assistant received | Share |
|---|---|
| Named (coded, label found) | 39.8% |
| Coded, no label found | 5.2% |
| Text only, removed for privacy | 55.0% |

With the resolver off, named falls to 6.5%. The text-only share is every
medication and allergy in this data plus uncoded conditions. Redaction is
right to remove upstream text (#207, #209), so the fix is not to keep it.
Synthetic Hospital is a worst case; Epic and most portal exports code
medications in RxNorm. A tester whose source sends text-only medications
will still get "recorded but not coded" for every one. Owner decision:
whether to map text to codes server-side before redaction, which sends that
text to a terminology service.

**3. Uploads fail under concurrent writes on SQLite.** 5 to 9 of 20 uploads
returned `commit_failed` on SQLite, a different set each run. The cause is
`database is locked` on `INSERT INTO r6_resources`: the ingest transaction
reads, then cannot upgrade to a write while the worker's run writes hold the
lock. Postgres: 0 failures in four full runs. Production is Postgres, but
`docker-compose.yml` self-hosting is SQLite. Changing engine transaction
behaviour needs CTO review under the sign-off standard, so it is filed
separately.

**4. Bare ids in `?patient=` are refused.** `?patient=sh-2300` returns 400
"Patient reference must match Patient/{id}". FHIR R4 allows a bare id on a
reference search parameter. The refusal is explicit, not silent, so clients
learn quickly; it is a conformance gap, not a safety one.

**5. Phone-width polish.** At 375 px the chat header wraps the assistant name
and all three pills, and the last pill reaches the screen edge with no
gutter. The "Upload a file" control renders as an unstyled native button.
Two upload connections carry the same label and cannot be told apart.

**6. Smaller items.** The chat rate limit (20 turns per 10 minutes) is held
in memory per web process, so with 2 gunicorn workers an account can get up
to 40. The worker logs "run claim failed" without the cause when HealthClaw
is not yet listening at boot.

## Not covered

- The Synthetic Hospital simulator itself and the upstream proxy path.
- Answer quality from a real model (no model key in this session).
- Passkey sign-in (email codes only), Fasten, wearables.
- Production's signed-in journey (needs a readable inbox, #242).

## Reproduce

```bash
python scripts/synthetic_hospital_cohort.py --data <release dir> --out /tmp/cohort
# Local stack: HealthClaw on :5300, CareAgents web on :5301 and its worker,
# CARE_ENV=development so sign-in codes are written to the CareAgents log.
python scripts/cohort_shakeout.py --cohort /tmp/cohort \
    --care http://localhost:5301 --care-log <careagents log> \
    --prompt-log <model request log> --healthclaw-db <HealthClaw DB URL>
python scripts/cohort_engine_probe.py --cohort /tmp/cohort \
    --base http://127.0.0.1:5300 --internal-secret <INTERNAL_TOKEN_MINT_SECRET> \
    --db <HealthClaw DB URL>
```
