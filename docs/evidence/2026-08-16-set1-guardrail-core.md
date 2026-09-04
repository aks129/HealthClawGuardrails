# Feature set 1 — Guardrail core — Wave-1 evidence pack

- **Date:** 2026-08-16
- **Owner:** owner-guardrail-core
- **Repo HEAD:** `2b7872d` (`feat(kernel): four step-up sites are predicates, not gates — has_grant is the half that fits (#506)`)
- **Working tree:** clean at start of run; this document is the only file added.
- **One redaction:** the operator's home-directory name in pasted shell output
  reads `<user>`. Nothing else in any transcript below is altered — the
  redaction is incidental to every claim the output supports.
- **Verdict: EVIDENCE PARTIAL.** Local half complete. Live-proxy half **BLOCKED** —
  the stack described in the task was not running when the run began, and
  starting it was outside the granted scope.

---

## 0. The blocker, first, because it changes how everything below reads

The task specified a live stack verified with `docker ps`: the guardrails app on
`:5099` in FHIR-proxy mode, Aidbox on `:8080`, MCP on `:3001`. **None of it was
reachable.** Docker Desktop's engine was down.

```
$ curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:5099/health
curl: (7) Failed to connect to localhost port 5099 after 0 ms: Couldn't connect to server
000
$ curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:8080/health
curl: (7) Failed to connect to localhost port 8080 after 0 ms: Couldn't connect to server
000
$ curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:3001/
curl: (7) Failed to connect to localhost port 3001 after 0 ms: Couldn't connect to server
000
```

Confirmed three independent ways rather than trusting one failed `curl`:

```
$ docker ps
failed to connect to the docker API at unix:///Users/<user>/.docker/run/docker.sock;
check if the path is correct and if the daemon is running: dial unix
/Users/<user>/.docker/run/docker.sock: connect: no such file or directory

$ ls -la ~/.docker/run/
total 0
drwxr-xr-x@  2 <user>  staff   64 Aug 16 17:16 .
drwxr-xr-x@ 22 <user>  staff  704 Aug 16 17:06 ..
        # the socket file is gone; the directory mtime is 17:16 today

$ lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(5099|8080|3001)\b'
        # no matches

$ uptime
17:21  up 8 days, 21:16, 1 user, load averages: 15.26 12.49 8.42
        # no reboot — Docker Desktop was quit or died at ~17:16 today
```

The task said explicitly: *do not start, stop, rebuild, or deploy anything.*
Bringing Docker Desktop and the compose project up is the thing that was
forbidden, so it was not done, and the live half is reported as blocked rather
than substituted for. **Standing order 1 (run it before you describe it) means
the proxy grade below is recorded as "not measured", not as "B".**

A second obstacle sits behind the first, and whoever restarts the stack will hit
it (see register **R-1**): the example's `.env` is absent, so compose cannot even
resolve its variables.

```
$ docker compose -f examples/aidbox-healthclaw-guardrails/docker-compose.yaml ps
error while interpolating services.healthclaw.environment.STEP_UP_SECRET:
required variable STEP_UP_SECRET is missing a value: set STEP_UP_SECRET in .env

$ test -f examples/aidbox-healthclaw-guardrails/.env && echo PRESENT || echo ABSENT
ABSENT
```

---

## 1. The two grades

| Mode | Expected | Measured this run | Evidence |
|---|---|---|---|
| Local (in-process harness) | Grade A, 7/7 | **Grade A, 7/7, 35 checks** | §1.1, §1.2 |
| Through the live proxy (`$conformance`) | Grade B, 6/7 | **NOT MEASURED — endpoint unreachable** | §0 |

The requester observed Grade B on the live endpoint before this session. That
observation is recorded here as *theirs*, not reproduced as mine. I did not run
it, so I do not report it as a result.

### 1.1 Local harness — pytest

```
$ uv run pytest tests/test_guardrail_conformance.py -q
...................................                                      [100%]
35 passed in 2.20s
```

All 35, by name (`-v`), because the interesting ones are the anti-vacuity tests:

```
test_our_deployment_records_full_conformance PASSED
test_the_scorecard_states_what_a_hitl_pass_does_not_prove PASSED
test_report_is_json_serializable PASSED
test_grade_scales_with_failures PASSED
test_report_exposes_property_grade_and_profile_coverage PASSED
test_report_serialization_preserves_legacy_implicit_grade PASSED
test_error_fidelity_grade_uses_the_worst_executed_profile PASSED
test_local_contract_rejects_false_a_evidence PASSED
test_corrective_outcome_rejects_unsafe_sibling_issue PASSED
test_lenient_warning_rejects_unsafe_sibling_outcome_entry PASSED
test_lenient_audit_requires_ignored_parameter_evidence PASSED
test_local_error_fidelity_records_grade_a PASSED
test_optional_mcp_profile_records_status_only_errors_as_c PASSED
test_optional_mcp_profile_accepts_flagged_operation_outcome PASSED
test_optional_mcp_profile_rejects_unsanitized_operation_outcome PASSED
test_malformed_mcp_result_remains_an_executed_profile PASSED
test_mcp_safe_outcome_plus_malformed_content_is_not_a PASSED
test_mcp_safe_outcome_in_non_text_content_block_is_not_a PASSED
test_audit_correlation_uses_new_event_ids_not_only_latest_entry PASSED
test_proxy_a_requires_a_corrective_operation_outcome PASSED
test_hostile_value_check_considers_parsed_body_and_raw_text PASSED
test_proxy_profile_does_not_echo_malformed_audit_codes PASSED
test_error_fidelity_crash_still_runs_configured_optional_profiles PASSED
test_live_mcp_probe_client_initializes_then_calls_tool PASSED
test_live_mcp_probe_client_accepts_stateless_json_transport PASSED
test_live_mcp_probe_client_decodes_sse_responses PASSED
test_local_error_probes_always_bound_searches_to_a_synthetic_subject PASSED
test_scripted_local_contract_can_reach_a_without_reading_real_data PASSED
test_injected_mock_proxy_profile_passes_the_full_error_contract PASSED
test_a_search_that_returns_nothing_no_longer_scores_a PASSED
test_step_up_validation_turned_off_no_longer_scores_a PASSED
test_a_deployment_that_refuses_everyone_no_longer_isolates PASSED
test_a_gate_that_blocks_confirmed_writes_too_is_not_a_gate PASSED
test_the_word_disclaimer_in_an_error_is_not_a_disclaimer PASSED
test_a_deployment_that_answers_nothing_scores_f PASSED

35 passed in 2.85s
```

### 1.2 Local harness — the rendered scorecard, all seven properties

Produced by calling the harness's documented public API
(`run_conformance(FlaskProbeClient(...), ProbeContext(...))`) against a
test-client app, mirroring `tests/conftest.py` exactly. No repo file was
modified; the driver script lives in the session scratchpad.

> The rendered scorecard below and the per-check dump in §2 come from **two
> separate invocations** of that driver. Each invocation builds a fresh
> in-memory database and creates new synthetic resources, so the resource UUIDs
> differ between the two excerpts. That is expected, not a transcription error —
> flagged here so the mismatch does not read as one.

```
HealthClaw Guardrail Conformance — local(test-client) [tenant=test-tenant]
  Grade: A   (7/7 properties)

  SCOPE: This grade covers the HealthClaw guardrail layer only (self-test,
  synthetic data). It is NOT a HIPAA Security Rule assessment, a third-party
  audit, or a penetration test of your deployment. Infrastructure, BAAs,
  encryption, and access controls are the deployer's responsibility. A third
  party can run this same harness against any instance as one input to a real
  assessment — it does not substitute for one.

  [PASS] PHI Redaction
        note: Patient/6a484c0b-db64-4c2e-9467-21003b3f9700
        ✓ synthetic patient created
        ✓ read succeeds — status 200
        ✓ the redacted record is still returned
        ✓ family name not returned in full
        ✓ given name not returned in full
        ✓ SSN-class identifier masked
        ✓ phone number stripped
        ✓ street address stripped
        ✓ search succeeds — status 200
        ✓ the search returns the resource it was asked for
        ✓ family name not returned in full (search)
        ✓ given name not returned in full (search)
        ✓ SSN-class identifier masked (search)
        ✓ phone number stripped (search)
        ✓ street address stripped (search)
        ✓ a recognised code is re-labelled after redaction — status 200; expected 'Cholesterol (total)'
        ✓ the upstream display did not survive
  [PASS] Immutable Audit Trail
        ✓ AuditEvent endpoint readable — status 200
        ✓ resource READ is recorded in the audit trail
        ✓ no raw SSN in the audit trail
  [PASS] Step-Up Authorization
        ✓ write without step-up token is rejected (401) — status 401
        ✓ write with a forged step-up token is rejected (401) — status 401
        ✓ write carrying a valid step-up token is accepted — status 201
  [PASS] Human-in-the-Loop
        note: the confirmation header is supplied by the probe: this grades the
              gate, not the human attestation behind it (#214)
        ✓ clinical write without human confirmation is blocked (428) — status 428
        ✓ confirmed clinical write is accepted — status 201
  [PASS] Tenant Isolation
        ✓ resource is not readable from another tenant — status 404, id=None
        ✓ resource IS readable from its own tenant — status 200
  [PASS] Medical Disclaimers
        note: Observation/8eb393bc-7034-4bb3-b287-bcbbb8a377ee
        ✓ clinical read carries a medical disclaimer
        ✓ the disclaimer accompanies the clinical record, not an error — status 200
  [PASS] Error Fidelity — A (local-fhir-only)
        local: run — A
          checks: strict unknown parameter is rejected, strict rejection is
                  audited as a failure, lenient unknown parameter carries an
                  outcome warning, lenient warning is audited truthfully,
                  unsupported modifier is rejected, unsupported modifier
                  rejections are audited as failures
        mcp: not_run
        proxy: not_run
        ✓ strict unknown parameter is rejected — grade A; status 400
        ✓ strict rejection is audited as a failure — grade A
        ✓ lenient unknown parameter carries an outcome warning — grade A; status 200
        ✓ lenient warning is audited truthfully — grade A
        ✓ unsupported modifier is rejected — grade A; statuses 400,400,400
        ✓ unsupported modifier rejections are audited as failures — grade A
```

**The property that fails in proxy mode is `error_fidelity`,** and the scorecard
above shows *why the two grades are not in conflict* without needing the live
run: local Grade A for that property carries `coverage=local-fhir-only`, with
`proxy: not_run`. The local A and the proxy failure are **measured over
different profiles**. Local A is not evidence that the proxy path works, and the
harness is explicit about that rather than rounding it up.

The mechanism itself (unknown search parameters forwarded to Aidbox instead of
refused by the guardrail with a corrective `OperationOutcome`) is documented in
**#498** and in the code comment at `examples/.../scripts/walkthrough.sh` step 4.
**I did not execute it this run**, so it is cited, not asserted as measured.

---

## 2. Did each passing probe's subject actually run?

This is the check the charter calls the likeliest lie, and it is the one place
this pack found something worth keeping.

**For local mode: yes, and it is enforced by tests, not by luck.** Every
property carries a positive control that would fail if its subject never
existed:

| Property | The positive control |
|---|---|
| PHI Redaction | `✓ synthetic patient created`, `✓ the redacted record is still returned`, `✓ the search returns the resource it was asked for` |
| Audit Trail | `✓ AuditEvent endpoint readable — status 200` before any PHI assertion |
| Step-Up | `✓ write carrying a valid step-up token is accepted — status 201` (the accept case, not only two refusals) |
| Human-in-the-Loop | `✓ confirmed clinical write is accepted — status 201` |
| Tenant Isolation | `✓ resource IS readable from its own tenant — status 200` |
| Medical Disclaimers | `✓ the disclaimer accompanies the clinical record, not an error — status 200` |
| Error Fidelity | `status 200` on the lenient probe — a warning riding a real result |

The per-check dump confirms these are real observations, not labels:

```
[tenant_isolation] passed=True grade=None coverage=full
    - PASS resource is not readable from another tenant
        observed: status 404, id=None
    - PASS resource IS readable from its own tenant
        observed: status 200

[medical_disclaimer] passed=True grade=None coverage=full
    note: Observation/4534a210-750a-42c9-8e79-df26c71b7fb2
    - PASS clinical read carries a medical disclaimer
    - PASS the disclaimer accompanies the clinical record, not an error
        observed: status 200
```

And the vacuity class is pinned by dedicated tests that fail if a probe is ever
weakened back into it — these are the six named in §1.1:

- `test_a_search_that_returns_nothing_no_longer_scores_a`
- `test_step_up_validation_turned_off_no_longer_scores_a`
- `test_a_deployment_that_refuses_everyone_no_longer_isolates`
- `test_a_gate_that_blocks_confirmed_writes_too_is_not_a_gate`
- `test_the_word_disclaimer_in_an_error_is_not_a_disclaimer`
- `test_local_error_probes_always_bound_searches_to_a_synthetic_subject`

**No vacuous pass was found in local mode.** The defect class this project keeps
finding has already been closed here, deliberately and with tests naming it.

**For live proxy mode: not checked.** This is the highest-value thing this pack
could not do, because in proxy mode the subject is created in *Aidbox*, and
whether Aidbox accepted it (vs. 422-ing on referential integrity while the probe
scored a pass) is exactly the question that cannot be answered from a local run.
Carried as **R-2**.

---

## 3. The write-gate matrix, all four rows

**Executed** — but against the application in-process via the Flask test client,
**not** through `localhost:5099` and **not** with Aidbox behind it, because of
§0. Same handler code, same `before_request` chain; no network hop, no upstream.
Labelled as such rather than presented as the live run that was asked for.

Token minted the way `walkthrough.sh` does it — `POST
/r6/fhir/internal/step-up-token` with the tenant header and a `tenant_id` body:

```
=== 0. Mint a step-up token the way walkthrough.sh does ===
    POST /r6/fhir/internal/step-up-token -> HTTP 200
    token minted: yes (len=201, value redacted)
```

```
=== 3. Write-gate matrix (four rows) ===
    neither gate                       -> HTTP 428   [PASS]
    X-Human-Confirmed only             -> HTTP 401   [PASS]
    X-Step-Up-Token only               -> HTTP 428   [PASS]
    both gates                         -> HTTP 201   [PASS]
    created Observation/be842cd5-1680-4316-8f66-b31d81980114
```

All four match the documented contract. The gate order holds as stated: a bare
write reports **428** (human gate), not 401, because `enforce_human_in_loop`
runs in `before_request`, ahead of every handler's auth gate. The two gates do
not substitute for each other — presenting either one alone still refuses, and
each refuses with *its own* status.

The same matrix is replayed against the app in CI, and that suite passes:

```
$ uv run pytest tests/test_aidbox_example_tells_the_truth.py -v
TestTheWalkthroughAssertsWhatTheServerReturns::test_every_row_matches PASSED
TestTheWalkthroughAssertsWhatTheServerReturns::test_the_two_gates_are_independent PASSED
TestTheReadmeAgreesWithTheScript::test_the_table_lists_the_same_statuses PASSED
TestTheComposeFileConfiguresThingsThatExist::test_every_variable_is_read_somewhere PASSED
TestTheActivationGateCanActuallyGate::test_the_aidbox_check_requires_a_200 PASSED
TestTheImagePinMatchesThisRepo::test_the_pinned_tag_is_this_version PASSED
TestTheImagePinMatchesThisRepo::test_no_image_is_floating PASSED

7 passed in 7.43s
```

**What this does not prove:** that the 201 lands in Aidbox. `walkthrough.sh`
step 3 checks that separately by querying Aidbox directly, going around the
proxy, precisely because a proxy reporting its own 201 says nothing about
storage. That check was not run. Carried as **R-2**.

---

## 4. Tenant isolation

**Executed** in-process, same caveat as §3. Positive control first: prove tenant
A can read the record, *then* prove tenant B cannot. Without the first half, a
404 for tenant B is satisfied by a record that was never created.

```
=== 4. Tenant isolation ===
    create Patient in tenant A -> HTTP 201
    Patient/baf75ac4-7282-47d5-abfb-9384f133bf75 belongs to tenant A
    tenant A reads its OWN record      -> HTTP 200   [PASS]
    -> resourceType=Patient id=baf75ac4-7282-47d5-abfb-9384f133bf75
    tenant B reads tenant A's record   -> HTTP 404   [PASS]
    cross-tenant response body:
      {"issue": [{"code": "not-found",
                  "diagnostics": "Patient/baf75ac4-7282-47d5-abfb-9384f133bf75 not found",
                  "severity": "error"}],
       "resourceType": "OperationOutcome"}
```

Tenant B held a **valid step-up token for tenant B** — this is not an
unauthenticated caller being turned away. A correctly-credentialed caller for
the wrong tenant gets 404, not 403, so the response does not confirm the
resource exists. The resource id echoed in `diagnostics` is the one the caller
supplied, not a disclosure.

---

## 5. Redaction, positive assertion first

**Executed** in-process, same caveat as §3. Synthetic values seeded by this run;
no real record was involved.

> Redaction behaviour changed on 2026-09-04 (#615): identifier values are now
> removed, not truncated to their last four characters. The transcript below
> shows the earlier shape and is left as-is — a dated record of what was true
> that day, not current behaviour.

```
=== 5. Redaction, positive assertion FIRST ===
    PASS received Patient/baf75ac4-7282-47d5-abfb-9384f133bf75 (not an OperationOutcome)
    PASS none of the seeded identifiers appear in the returned record
    returned record (whole body, synthetic):
      {"address": [{}],
       "birthDate": "1974",
       "id": "baf75ac4-7282-47d5-abfb-9384f133bf75",
       "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn", "value": "***6789"}],
       "meta": {"lastUpdated": "2026-08-16T21:20:17.473Z", "versionId": "1"},
       "name": [{"family": "T.", "given": ["S."]}],
       "resourceType": "Patient",
       "telecom": [{"system": "phone", "value": "[Redacted]"}]}
```

Order matters and was observed: **resourceType is `Patient`, not
`OperationOutcome`** — so the assertions that follow are being made about a
record, not about a refusal. Then: family name reduced to an initial, given name
to an initial, `birthDate` truncated to year, address object emptied, phone
replaced, SSN masked to `***6789`.

Asserted on the seeded *values* (`Testpatient`, `Synthetic`, `123-45-6789`,
`221 Baker St`, `555-867-5309`, `1974-03-11`), not on the shape of the mask.
Checking for a `[Redacted]` marker would pass if redaction were replaced by a
function that returned that string and nothing else.

Supporting suites, all green:

```
$ uv run pytest tests/test_healthclaw_redact.py tests/test_recursive_redaction.py \
    tests/test_redaction_coverage_inventory.py tests/test_redaction_probes_multistep.py \
    tests/test_access_kernel.py tests/test_tenant_read_boundaries.py \
    tests/test_step_up_states_why.py tests/test_audit_coverage_gate.py \
    tests/test_audit_failure_posture.py tests/test_ungraded_is_published.py -q
192 passed in 17.84s
```

---

## Edge-case register

What does not work, in writing.

| ID | Finding | Severity | Issue |
|---|---|---|---|
| **R-1** | **The example stack cannot be restarted as-is.** `examples/aidbox-healthclaw-guardrails/.env` is absent, and compose interpolation fails hard on the required `STEP_UP_SECRET` before any container starts. Whoever restarts the stack must recreate that file first. The stack that was running earlier today therefore had its env from somewhere no longer on disk. | Medium — blocks the next live run | none yet |
| **R-2** | **No live-proxy evidence exists for set 1 as of this pack.** Grade B is unverified by me; the proxy `error_fidelity` failure is cited from #498, not measured; and the "did the probe's subject actually get created in Aidbox" question of §2 is unanswered for proxy mode. This is the gap most worth closing next, because it is the only mode where a probe can pass on a subject Aidbox rejected. | High — it is the pack's missing half | #498 (the failure), none for the verification gap |
| **R-3** | **Error fidelity degrades in upstream proxy mode.** Unknown search parameters and unsupported modifiers are forwarded to Aidbox, which answers 404/502, instead of being refused by the guardrail with a corrective `OperationOutcome` naming the parameter. An agent cannot self-correct from a 502. The proxy path in `r6/fhir_proxy.py` forwards the query without applying the strict/lenient parameter handling that the local search path has. Cited, not measured this run. | High — it is the named 7th property | **#498** |
| **R-4** | **`X-Human-Confirmed` remains the entire human gate for direct clinical FHIR writes.** The 428→201 transition in §3 proves the gate *discriminates*; it does not prove a human attested to anything, because the header is supplied by the caller. The action rail's separate approval endpoint is the real mechanism. The scorecard states this limit on its own face rather than hiding it. Do not build new write paths on the header. | High — known, disclosed | **#214** |
| **R-5** | **Read authentication is not graded by the harness at all.** `READ_AUTH_ENABLED` defaults off and is off in the harness fixture, so a deployment serving records to strangers scores what a gated one scores. Verified as *deliberate and current*: #401 was closed COMPLETED on 2026-08-11 by **publishing** the exclusion beside the grade (PR #482), not by adding a probe — a probe that passes when read auth is off would reproduce the very defect. `tests/test_ungraded_is_published.py` fails if a key appears in both tuples. Not a stale exclusion. | Medium — disclosed, not fixed | #401 (closed by disclosure) |
| **R-6** | **The redaction profile retains the last four characters of an identifier**, including SSN-class ones (`***6789` above). This is documented intent (`r6/redaction.py`: "Identifiers: Keep last 4 characters"), not a bug — but HIPAA Safe Harbor removes SSN in full, so any claim of Safe Harbor de-identification would be wrong as configured. Worth an explicit line wherever the redaction claim is made to partners. | Low — correctness of the *claim*, not the code | none yet |
| **R-7** | **`walkthrough.sh` ships a hardcoded default Aidbox credential** (`AIDBOX_SECRET="${AIDBOX_SECRET:-<set>}"`) in a public repo. It is a local-demo default, not a production secret, and it is overridable by env — but a committed default credential is the kind of thing an adopter copies. Redacted here rather than reproduced. | Low | none yet |
| **R-8** | **Local Grade A for `error_fidelity` covers one profile only** (`coverage=local-fhir-only`; `mcp: not_run`, `proxy: not_run`). A reader who sees "Grade A, 7/7" and stops there will over-read it. The scorecard does print the coverage string, so this is a reading hazard rather than a dishonest report. | Informational | none |

Nothing in this pack required a code change, and none was made. No pin was
touched.

---

## What I did NOT check

Stated plainly, because scope stated honestly beats coverage implied.

1. **The live `$conformance` endpoint.** Never reached. The Grade B in the task
   brief is the requester's prior observation, not my measurement. I report the
   proxy grade as *not measured*.
2. **Anything through `localhost:5099`.** Every status code in §3, §4 and §5 came
   from the Flask test client against the same application code, with no network
   hop and no Aidbox upstream. They are consistent with the documented contract
   and with CI, but they are not the live-proxy run that was asked for.
3. **Aidbox itself.** Not reached. Specifically not checked: whether a proxied
   write lands in Aidbox (`walkthrough.sh` step 3's around-the-proxy query);
   whether Aidbox refuses anonymous callers (step 0's `BOX_SECURITY_DEV_MODE`
   assertion); whether conformance probe subjects are actually created upstream
   rather than 422-ing on referential integrity.
4. **The MCP server on `:3001`.** Not reached. Neither the unauthenticated
   `tools/list` → 401 assertion nor the authenticated tool-surface count
   (`walkthrough.sh` step 5). The `mcp` error-fidelity profile shows `not_run`.
   Note this is the exact component that, in the incident the charter cites, had
   never started while a diagram claimed it had.
5. **The recording artifact.** `examples/aidbox-healthclaw-guardrails/qa/`
   (Playwright) was not run — it drives the live stack, which is down. Per the
   PHI rules, a run that cannot be recorded is still run and the assertion log
   is the evidence; that is what §1–§5 are. **The recording is one of the four
   owed artifacts and it is missing.**
6. **The two sign-offs.** QA's adversarial pass and the end-user pass have not
   happened. **Two of the four owed artifacts are therefore outstanding**: the
   recording and the sign-offs. Run log and edge-case register are delivered.
7. **The full test suite and `ruff`.** Only the set-1 suites listed above were
   run (192 passed) plus the conformance harness (35) and the example-truth
   suite (7). `uv run pytest -q` across the whole repo and `uv run ruff check .`
   were not run, because nothing is being pushed from this pack.
8. **Medplum.** The charter names it as a real-data source alongside Aidbox. Not
   touched at all.
9. **Non-FHIR surfaces.** No Telegram, no CareAgents, no action rail, no
   `/r6-dashboard`. Set 1's boundary was respected; nothing outside it was
   widened into.

---

## To close this pack

1. Restore `examples/aidbox-healthclaw-guardrails/.env` (**R-1**), bring the
   compose project up, and re-run §1 (`$conformance` live), §3, §4, §5 against
   `localhost:5099` with `scripts/walkthrough.sh`.
2. Answer §2 for proxy mode: for each property the live endpoint reports PASSED,
   confirm the subject exists **in Aidbox** (**R-2**).
3. Run `qa/demo.spec.ts` to produce the asserting recording.
4. Collect the QA and end-user sign-offs.

Until then this set is **EVIDENCE PARTIAL**, and the honest summary is: the
guardrail core is verified against the application code, and unverified against
the deployment.
