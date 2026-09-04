# Feature set 2 — connectors: the "2 of 4" claim, re-run by someone else

**Run by:** QA (not the author of the 2026-08-16 pack) · **Date:** 2026-09-04 ·
**Verdict: THE CLAIM REPRODUCES, with one step now passing that failed before.**

Issue #530. Three merged documents assert that two of four connector kinds are
proven live. Until today that rested on a re-pointed copy of the Aidbox
walkthrough living in an uncommitted scratch directory — the 2026-08-16 pack
says so itself, in register entry R8 — so the most load-bearing measured claim
in the process documents sat in the document whose thesis is that
unreproducible assertions are the defect.

The scratch directory is gone. Nothing was recovered from it; the script below
was written fresh from the pack's transcripts, which is itself the check on
whether those transcripts describe something reproducible. They do.

## What is now committed

| Artifact | Path |
|---|---|
| The walkthrough | `scripts/walkthrough-upstream.sh` |
| HAPI transcript | `docs/evidence/2026-09-04-set2-rerun/hapi-run.txt` |
| Firely transcript | `docs/evidence/2026-09-04-set2-rerun/generic-run.txt` |
| Negative control | `docs/evidence/2026-09-04-set2-rerun/negative-control-local-mode.txt` |

Run it:

```
export FHIR_UPSTREAM_KIND=hapi FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4
export READ_AUTH_ENABLED=true STEP_UP_SECRET=dev-secret APP_ENV=development
export SQLALCHEMY_DATABASE_URI=sqlite:////tmp/hc-hapi.db PORT=5099
uv run flask --app main init-db && uv run python main.py &
scripts/walkthrough-upstream.sh hapi
```

and the same with `FHIR_UPSTREAM_KIND=generic`,
`FHIR_UPSTREAM_URL=https://server.fire.ly/R4`, `walkthrough-upstream.sh generic`.

One parametrized script rather than the `walkthrough-hapi.sh` plus near-copy
that #530 asks for. Two copies of one assertion list is how the Aidbox script's
status codes drifted from its own README (#499), and a second copy would need
its own "tells the truth" pin to stop it drifting again.

## The environment differs from the pack's in one way that matters

Both runs used the source tree at `89b42fb` via `uv run python main.py`, with a
throwaway SQLite database outside the repository. The pack ran at `2b7872d`.

Between the two, four defects the pack found were fixed and merged: #512
(`hapi` dropping its credentials), #513 (health reporting `upstream` while
writes landed in SQLite), #514 (`$conformance` colliding on a shared server),
#518 (an unknown kind booting then 500-ing). **#514 is why one line of the HAPI
result is different, and better.**

Docker was down again, exactly as on 2026-08-16, so the MCP server did not run
in either walkthrough. Step 5 is red for absence in both. That is not a
connector finding either way.

## Both upstreams answered

| Server | `/metadata` | Version |
|---|---|---|
| `hapi.fhir.org/baseR4` | HTTP 200 in 0.98s | HAPI FHIR Server, FHIR 4.0.1 |
| `server.fire.ly/R4` | HTTP 200 in 7.46s | Firely Server, FHIR 4.0.1 |

Neither was down, so neither run has an outage to discount. Firely's 7.5s is
why every request in the script carries `--max-time 60`: a 10s timeout would
report a working server as unreachable.

## Step-by-step, pack against today

`≡` means the line matched the 2026-08-16 pack.

| Step | `hapi` (2026-08-16) | `hapi` (today) | `generic` (2026-08-16) | `generic` (today) |
|---|---|---|---|---|
| 0 preflight — mode, kind, connected | PASS | ≡ PASS | PASS | ≡ PASS |
| 0 anonymous upstream | NOTE | ≡ NOTE | NOTE | ≡ NOTE |
| 1a create reached upstream | PASS | ≡ PASS | PASS | ≡ PASS |
| 1b gate matrix 428/401/428/201 | PASS ×4 | ≡ PASS ×4 | PASS ×4 | ≡ PASS ×4 |
| 1b Observation landed upstream | PASS | ≡ PASS | PASS | ≡ PASS |
| 2 redaction + `_source: upstream` | PASS | ≡ PASS | PASS | ≡ PASS |
| 3 audit written, PHI-free | PASS (5 entries) | PASS (3 entries) | PASS (3) | ≡ PASS (3) |
| 4 `$conformance` | **FAIL — grade F 1/7** | **PASS — grade B 6/7** | PASS — B 6/7 | ≡ PASS — B 6/7 |
| 5 MCP `tools/list` | FAIL — HTTP 000 | ≡ FAIL — HTTP 000 | FAIL — HTTP 000 | ≡ FAIL — HTTP 000 |

Every guardrail assertion the claim rests on held again, on a different day,
from a script written by someone who did not write the first one, against the
same two public servers.

Two rows are not `≡`, and neither is a guardrail difference:

**Step 4 on HAPI: F 1/7 → B 6/7.** This is #514 landing.
`_synthetic_patient()` now carries a `uuid4` identifier, so HAPI's
duplicate-detection interceptor no longer answers the second and later probe
creates with 412 `HAPI-2840`. The pack's diagnosis was that the F was caused by
the harness and not by the guardrails, and that the same harness grading B
against Firely was the control confirming it. Both halves are now confirmed
directly: with the harness fixed and nothing else changed, HAPI grades what
Firely graded. The two servers now agree, and `error_fidelity` (#498) is the
only failure on either.

**Step 3 audit count on HAPI: 5 → 3.** The pack's HAPI run happened after
earlier attempts against the same scratch database (the pack records a first
§3 run "before the script was corrected"). Today's runs each used a fresh
database, so both show 3. The assertion is that entries exist and carry no PHI,
not how many; the count is reported because it moved.

## The script can fail

A walkthrough that has never gone red is a demo of itself. The `_source`
assertion in step 2 is the one line separating this run from the false pass the
pack found in `smoke_medplum.py` — 7 of 8 checks green against a Medplum that
did not exist (R2) — so the same trap was set for this script deliberately.

With no upstream configured, the app running in local mode, and the script
still told `hapi`:

```
0. Preflight
    proxy version 1.10.0, mode local

The proxy is running in LOCAL mode — it is not talking to hapi at all.
Everything below would pass against the proxy's own SQLite store and prove
nothing about the connector. Check FHIR_UPSTREAM_URL is exported.
```

Exit 2 at step 0. No request reached either public server, and no step reported
a pass. Full transcript in `negative-control-local-mode.txt`.

## What this run does NOT prove

Unchanged from the 2026-08-16 pack except where noted:

- **`aidbox` and `medplum`, in any respect.** Not attempted. Docker is still
  down on this machine and no Medplum client credentials exist here. The
  four-row table is still two rows of yes.
- **`generic` with HTTP Basic against a server that requires it.** Firely's
  public server takes no credential, so this exercised the anonymous branch
  again. The Basic branch was proven on the wire in the pack's §6 and was not
  re-tested here.
- **`hapi` with a credential.** Same reason. #512 changed `hapi` to
  `AUTH_BASIC` after the pack found the drop; this run does not exercise that
  fix, because a public sandbox cannot. The unit tests cover it.
- **The "proxy holds its own credential" property**, for either kind. Both
  upstreams serve anonymous callers, which the script reports as a NOTE rather
  than asserting a guaranteed red.
- **The MCP server**, in either run. Docker is down. Step 5 is red for that
  reason and no other.
- **The pinned container images.** Both runs used the source tree, not the
  1.10.0 images.
- **The `qa/demo.spec.ts` recording**, still missing, still needs the compose
  stack. The 2026-08-16 pack is EVIDENCE PARTIAL for that reason and this run
  does not change it.
- **Cleanup of synthetic resources on the public servers.** One Patient and one
  Observation on each, plus whatever `$conformance` created on each during its
  probe run. All synthetic, no PHI, left in place — same footprint as the
  pack's day. Today's carry nonces `86425f1cde39` (HAPI) and `471213ce4181`
  (Firely).
- **Production was not touched.** The two Flask processes were ephemeral, bound
  to localhost:5099, used scratch SQLite databases outside the repository, and
  were stopped at the end.

## What this changes in the 2026-08-16 pack

Two of its statements are now false, and are annotated there rather than
rewritten — the findings of that day stand as that day's findings:

- **R8** — "There is no `hapi` or `generic` walkthrough in the repo" — closed by
  `scripts/walkthrough-upstream.sh`.
- **"Reproducing this"** — the scratch directory it names is gone. The committed
  paths above replace it.

Its §3 Grade F for HAPI and hard-truths §4's "graded F, 1/7" row are dated
observations that were true on 2026-08-16, and stay as written.
