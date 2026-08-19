# HealthClaw Guardrail Specification

**Version 0.1.0-draft** · 2026-08-16 · Issue [#234](https://github.com/aks129/HealthClawGuardrails/issues/234)

**Status: draft. Nothing in this document has been agreed with an external
party, and no deployment has yet been certified against it.**

This is the normative contract for an enforcement layer that sits between an AI
agent and a FHIR record server: reads come back redacted, every access is
audited, writes need a credential the agent cannot mint, clinical writes need a
second party, and one tenant cannot see another's data.

It exists because a third party adopting HealthClaw today has no document to
conform to beyond reading our source. The de facto contract is seven properties
in a test file (`tests/test_guardrail_conformance.py`) and the probes behind
them (`r6/conformance/probes.py`). This document promotes that contract to a
stated one, and — more importantly — states where the test file is narrower than
the property it is named after.

## The rule this document was written under

> No claim here may be stronger than what `tests/test_guardrail_conformance.py`
> actually verifies. Where the requirement exceeds the suite, it is marked
> **aspirational** and the gap is named.

Every property section therefore has two halves that are allowed to disagree in
public: the **normative requirement** an implementer builds to, and **what
0.1.0's suite actually checks**. Where they diverge, the divergence is written
down. A specification that hid that would join a list this project keeps
internally of its own polished artifacts making claims the code did not
support — a flagship conformance report that graded F against a real server, a
security policy that claimed a legal de-identification standard, a QA script
reporting "7/8 checks passed" against a server that was not running. In every
case the polish is what suppressed inspection.

---

## 1. Conformance model

### 1.1 Terminology

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be interpreted
as described in RFC 2119 and RFC 8174, when and only when they appear in
capitals.

- **Implementation** — the guardrail layer under test.
- **Agent** — a non-human caller holding a credential, acting on a user's
  behalf. The primary subject of this specification.
- **Tenant** — the isolation boundary. Every request carries exactly one.
- **Upstream** — a FHIR server the implementation proxies to, when configured.
- **Graded surface** — the request paths this suite probes. In HealthClaw that
  is the FHIR route family under `/r6/fhir`. See §6.2: it is not the whole read
  surface, and that matters more than anything else in this document.

### 1.2 The system under test

An implementation conforms at a **deployment**, not at a commit. Configuration
is part of what is graded: a deployment with read authentication disabled and a
deployment with it enabled are different systems under test, and this version of
the suite cannot tell them apart (§4, [#401](https://github.com/aks129/HealthClawGuardrails/issues/401)).

The suite probes over HTTP with synthetic data only. It **writes**, and it does
not clean up. An implementation being graded MUST designate a self-test tenant
for it; pointing the suite at a demo or production tenant leaves probe records
behind, which has happened here and was found on camera the day before a launch
recording ([#463](https://github.com/aks129/HealthClawGuardrails/issues/463)).

### 1.3 Profiles

A property MAY be measured over more than one profile. Three are defined:

| Profile | What it exercises | Default |
|---|---|---|
| `local` | the implementation answering from its own store | run |
| `proxy` | the implementation in front of a configured upstream | `not_run` |
| `mcp` | the implementation's MCP tool surface | `not_run` |

A property graded over one profile says nothing about the others. A report MUST
declare which profiles ran (§3, R1). HealthClaw's own `error_fidelity` grade
carries `coverage=local-fhir-only` with `proxy: not_run` for exactly this
reason.

### 1.4 Grading

The overall grade is a pure fraction of properties passed:

```
7/7 -> A    6/7 -> B    5/7 -> C    4/7 -> D    <=3/7 -> F
```

Executed against `r6.conformance.probes._grade` on 2026-08-16 to confirm the
boundaries at a 7-property total.

**The properties are not equally weighted, and the grade does not know that.**
A deployment that loses tenant isolation scores B. A deployment that stops
attaching a medical disclaimer also scores B. Those are not comparable failures.
This is why:

> A published grade below A MUST name the failing property (§3, R2). "Grade B"
> alone is not a conformance claim.

### 1.5 What a Grade A attests, and what it does not

A conforming report MUST carry a scope statement to this effect
(`probes.SCOPE_STATEMENT`):

> This grade covers the guardrail layer only (self-test, synthetic data). It is
> NOT a HIPAA Security Rule assessment, a third-party audit, or a penetration
> test of the deployment. Infrastructure, BAAs, encryption, and access controls
> are the deployer's responsibility. A third party can run this same harness
> against any instance as one input to a real assessment — it does not
> substitute for one.

Grade A additionally does **not** attest:

- that the graded route family is the only way records leave the deployment
  (§6.2);
- that a human confirmed anything (§2, P4);
- that an unauthenticated caller cannot read (§4);
- that any profile other than the ones listed as `run` behaves this way.

### 1.6 Claim status markers

Every normative requirement below carries one:

| Marker | Meaning |
|---|---|
| **[H]** | verified by the 0.1.0 conformance suite; the check names are given |
| **[I]** | enforced by the implementation's internal tests, not by this suite |
| **[A]** | aspirational — stated as the requirement, not currently verified anywhere |

**[A]** is not a promise of a date. It is a statement that the requirement is
the right one and the evidence is absent.

---

## 2. The seven properties

### P1 — PHI redaction

**Normative requirement**

1. A read or search response returned to an agent MUST NOT contain, in the form
   the record holds them: family names, given names, identifier values, telecom
   values, or street address lines. **[H]**
2. The response MUST still be the resource that was requested. An empty or
   refused response does not satisfy (1). **[H]**
3. Free-text fields supplied by an upstream or a caller (`display`,
   `CodeableConcept.text`, narrative `text.div`, `note`) MUST be removed before
   the response is assembled. **[H]**
4. Any human-readable label on a coded element MUST be derived from the code by
   the implementation's own terminology table, applied **after** (3). An
   upstream label MUST NOT be preserved to make a record readable. **[H]**
5. If the implementation cannot apply its redaction profile, it MUST refuse the
   read. It MUST NOT return the record unredacted. **[A]**

Requirement 4 is the one that looks like decoration and is not. Real feeds put
patient names in `display` and `CodeableConcept.text`. An implementation that
preserves them to keep records readable passes every absence check in this
property while leaking, and it has been got wrong in this codebase more than
once ([#367](https://github.com/aks129/HealthClawGuardrails/issues/367), [#382](https://github.com/aks129/HealthClawGuardrails/issues/382)).

**How it is observed from outside**

```
POST /r6/fhir/Patient            (step-up token, synthetic body carrying
                                  five distinctive tokens)
GET  /r6/fhir/Patient/{id}       -> 200, resourceType=Patient, id={id}
                                    and none of the five tokens present
GET  /r6/fhir/Patient?_id={id}   -> 200, Bundle CONTAINING that Patient,
                                    and none of the five tokens present
POST /r6/fhir/Observation        (code LOINC 2093-3, display "UPSTREAM-
                                  DISPLAY-MUST-NOT-SURVIVE")
GET  /r6/fhir/Observation/{id}   -> 200, carries the implementation's own
                                    label for 2093-3, and NOT the junk
```

Assert the positive first. The absence checks are all satisfied by a response
that returned nothing, and "search returns nothing" is a live outage that scored
A here until a positive control was added ([#376](https://github.com/aks129/HealthClawGuardrails/issues/376)).

**What a conforming refusal looks like**

A refusal is an `OperationOutcome`. It MAY name the resource type and the reason
class. It MUST NOT echo any field value from the record it refused to return.

**Non-goals for this property**

- Not de-identification for research release. The HealthClaw profile is
  Safe-Harbor-*style* field redaction; it is **not a legal de-identification
  determination** ([#112](https://github.com/aks129/HealthClawGuardrails/issues/112)), and see §6.4 on the truncation
  boundary.
- Not narrative analysis. Narrative is replaced wholesale, not scrubbed.
- Not applicable to a write echo: a create response returning content the caller
  just sent is out of scope here ([#380](https://github.com/aks129/HealthClawGuardrails/issues/380)).
- Not a statement about routes outside the graded surface (§6.2).

**What 0.1.0 verifies** — `probe_phi_redaction`, 17 checks: `synthetic patient
created`, `read succeeds`, `the redacted record is still returned`, five
absences on read (family name, given name, SSN-class identifier, phone, street),
`search succeeds`, `the search returns the resource it was asked for`, the same
five absences on search, `a recognised code is re-labelled after redaction`,
`the upstream display did not survive`. 3 + 5 + 2 + 5 + 2 = 17.

**Divergences from the requirement**

| Divergence | Consequence |
|---|---|
| The specific transformation (name to initial, identifier to last four, birthDate to year) is **implementation detail, not normative**. A conforming implementation MAY remove these fields entirely, which is stronger. | HealthClaw's own choice is weaker than removal. §6.4. |
| The suite checks five token values on one resource type. | A field the profile misses on another resource type is not detected. |
| Requirement 5 (refuse rather than return raw) is unprobed. | **[A]** |

---

### P2 — Audit trail

**Normative requirement**

1. Every access to a record through the guardrailed API MUST emit an audit
   record. **[I]**
2. That record MUST be retrievable by the owning tenant through the same API.
   **[H]**
3. A read MUST be distinguishable from a write in the record. **[H]**
4. Audit detail MUST NOT contain PHI. **[H]** for one token class; **[I]**
   otherwise.
5. An access that could not be audited MUST NOT be reported as a success. **[I]**
   (`tests/test_audit_failure_posture.py`: a failed audit write raises rather
   than proceeding unrecorded.)
6. An audit record MUST NOT be modifiable or deletable through the API. **[A]**

**How it is observed from outside**

```
POST /r6/fhir/Patient                -> 201
GET  /r6/fhir/Patient/{id}           -> 200
GET  /r6/fhir/AuditEvent?_count=100  -> 200, Bundle containing an AuditEvent
                                        with action=R whose entity references
                                        Patient/{id}
```

Matching only the resource reference is not sufficient: the create above already
emits an event referencing the same string, so a reference-only check passes
with read auditing deleted entirely. The action discriminator is the check.

**What a conforming refusal looks like**

If the audit query is not available to the caller, the implementation MUST say
so with an `OperationOutcome`. It MUST NOT return an empty Bundle, which is
indistinguishable from "nothing was audited".

**Non-goals**

- Not a retention or export requirement.
- Not a completeness claim over non-FHIR surfaces.
- Not a claim about the audit store's physical immutability.

**What 0.1.0 verifies** — `probe_audit_trail`, 3 checks: `AuditEvent endpoint
readable`, `resource READ is recorded in the audit trail`, `no raw SSN in the
audit trail`.

**Divergences from the requirement**

| Divergence | Consequence |
|---|---|
| The property is titled "Immutable Audit Trail" in the report. **No probe attempts to modify or delete an AuditEvent.** | The word "Immutable" in a passing scorecard is not verified by this suite. Requirement 6 is **[A]**. Register **G-B**. |
| Requirement 1 says *every* access. The suite verifies *one* read. | "Every access" rests on internal coverage tests and `r6.access.install_audit_assertions`, which has a known false-positive class ([#321](https://github.com/aks129/HealthClawGuardrails/issues/321)). |
| The PHI check scans the returned Bundle for one synthetic SSN. | PHI entering through an adjacent column is not detected. `AuditEventRecord.resource_id` accepts unvalidated caller-supplied text ([#279](https://github.com/aks129/HealthClawGuardrails/issues/279)). |

---

### P3 — Step-up authorization

**Normative requirement**

1. A write MUST be refused unless it carries a credential the implementation
   issued and can verify. **[H]**
2. The gate MUST discriminate. A credential that is not the one the
   implementation issued MUST be refused, and a valid one MUST be accepted.
   **[H]**
3. The refusal status MUST be 401 or 403. It MUST NOT be 2xx. **[H]** (the
   suite requires exactly 401; see divergences)
4. A refusal SHOULD name the reason class — expired, wrong scope, wrong
   operation — so a caller can repair it. It MUST NOT publish a reason that
   describes a credential the caller does not hold. **[I]**
   (`tests/test_step_up_states_why.py`: "Token tenant mismatch" is deliberately
   never published, because it confirms to a probing caller that a token is real
   and merely issued elsewhere.)
5. The credential MUST NOT be mintable by the agent it authorizes. **[A]**

Requirement 2 is what separates "validates the token" from "notices a header".
The probe tampers with one byte of the issued token, which requires no knowledge
of the token format: whatever the deployment issued, this is not it.

**Implementation note, normative for callers of the HealthClaw kernel**

`validate_step_up_token` returns `(bool, str)`. A truthiness test on the tuple is
a silent authorization bypass, because a two-element tuple is always truthy. New
code calls `r6.access.require_grant`, which raises, or `has_grant`, which
answers. Neither returns a tuple. This is an interface hazard, not a property of
the specification, and it is recorded here because it is the shape that has
caused a bypass in this codebase.

**How it is observed from outside**

```
POST /r6/fhir/Patient   headers: tenant only              -> 401
POST /r6/fhir/Patient   headers: tenant + tampered token  -> 401
POST /r6/fhir/Patient   headers: tenant + issued token    -> 201
```

**What a conforming refusal looks like**

`OperationOutcome`, severity `error`, code `security` or `login`. It MUST NOT
echo the presented credential or any part of it. It MAY name the reason class
per requirement 4.

**Non-goals**

- Not a token-format specification. Any credential the implementation can issue
  and verify satisfies this property.
- Not a session or refresh model.
- Not a check of expiry, scope, replay, or cross-tenant refusal. Those are
  ungraded (§4) and verified only by internal unit tests.

**What 0.1.0 verifies** — `probe_step_up_enforcement`, 3 checks: `write without
step-up token is rejected (401)`, `write with a forged step-up token is rejected
(401)`, `write carrying a valid step-up token is accepted`.

**Divergences from the requirement**

| Divergence | Consequence |
|---|---|
| The suite requires status exactly `401`. The requirement allows 401 or 403. | A conforming implementation answering 403 **fails** this probe. Portability blocker; §8. |
| Requirement 5 (unmintable by the agent) is unprobed and, in one HealthClaw surface, false: the token is rendered into the patient's browser and served cross-origin ([#395](https://github.com/aks129/HealthClawGuardrails/issues/395)). | **[A]**, and see §5.3. |

---

### P4 — Human confirmation on clinical writes

This is the property whose requirement and whose evidence diverge most, and the
divergence is the reason this section exists.

**Normative requirement**

1. A write of a clinical resource MUST require a confirmation the requesting
   agent cannot produce by itself. **[A]**
2. That confirmation MUST originate with a different party, over a channel the
   agent does not control, and MUST be bound to the specific proposed content.
   **[A]**
3. An unconfirmed clinical write MUST be refused with 428 and MUST be
   distinguishable from an authorization failure. **[H]**
4. A confirmed clinical write MUST be accepted. A gate that blocks everything
   gates nothing. **[H]**
5. The two gates MUST NOT substitute for each other: presenting either alone
   MUST still refuse, each with its own status. **[I]**
   (`tests/test_aidbox_example_tells_the_truth.py`,
   `TestTheWalkthroughAssertsWhatTheServerReturns::test_the_two_gates_are_independent`)

**Requirements 1 and 2 are not met by direct FHIR writes in HealthClaw
today.** The gate is `X-Human-Confirmed: true`, a header the caller sets. An
agent holding a write step-up token self-confirms by adding one header
([#214](https://github.com/aks129/HealthClawGuardrails/issues/214)). The action rail's separate approval endpoint is the
real mechanism; the header is a known gap, and no new write path should be built
on it.

**How it is observed from outside**

```
POST /r6/fhir/Observation  tenant + step-up, no confirmation  -> 428
POST /r6/fhir/Observation  tenant + step-up + confirmation    -> 201
```

The 428/201 pair proves the gate **discriminates**. It does not prove a human
attested to anything, because the probe supplies the confirmation itself. A
conforming report MUST carry that limit on its face; HealthClaw's scorecard
prints it as a note naming #214, pinned by
`test_the_scorecard_states_what_a_hitl_pass_does_not_prove`.

**What a conforming refusal looks like**

428 Precondition Required, `OperationOutcome`, code `business-rule`, with
diagnostics naming what confirmation is required and where to obtain it. It MUST
NOT instruct the caller to satisfy the gate itself. HealthClaw's current
diagnostics reads *"Set X-Human-Confirmed: true header to proceed"*, which is an
accurate description of the deployed gate and, in one sentence, the whole of
requirement 1's gap.

**Non-goals**

- Not an attestation record. This property does not require the confirming
  party's identity to be captured. (It should. **[A]**)
- Not applicable to demographic writes.
- Not a specification of which resource types are clinical. HealthClaw's set is
  in `r6/health_compliance.py::CLINICAL_RESOURCE_TYPES`; an implementation
  declares its own.

**What 0.1.0 verifies** — `probe_human_in_the_loop`, 2 checks: `clinical write
without human confirmation is blocked (428)`, `confirmed clinical write is
accepted`, plus a mandatory note naming #214.

**Divergence, stated once more because it is the largest in this document**

The suite verifies a **discrimination floor**: the gate distinguishes a request
carrying the confirmation header from one that does not. The requirement is
**out-of-band confirmation by a second party**. Between the floor and the
requirement sits the entire security value of the property. Read the 428 as "this deployment has a gate here", not as
"a human approved this".

---

### P5 — Tenant isolation

**Normative requirement**

1. A resource created under tenant A MUST NOT be returned to a caller
   presenting tenant B, even when that caller holds valid credentials for B.
   **[H]**
2. The owning tenant MUST still receive it. A deployment that returns nothing to
   anyone isolates perfectly and serves nobody. **[H]**
3. The cross-tenant refusal SHOULD NOT confirm the resource exists. 404 is
   preferred to 403. **[I]** — not checked by the conformance suite; pinned by
   `tests/test_r6_routes.py::test_tenant_isolation_prevents_cross_tenant_read`,
   which asserts exactly 404 for an authenticated caller of another tenant.
4. Isolation MUST hold for search and aggregate paths, not only read-by-id.
   **[A]**

**How it is observed from outside**

```
POST /r6/fhir/Patient           tenant A            -> 201, id
GET  /r6/fhir/Patient/{id}      tenant A            -> 200, that Patient
GET  /r6/fhir/Patient/{id}      tenant B            -> 404
```

Positive control first, and in that order. A 404 for tenant B is satisfied by a
record that was never created.

**What a conforming refusal looks like**

404 with an `OperationOutcome` of severity `error`, code `not-found`. Echoing the
resource id the caller supplied is not a disclosure — the caller supplied it. Any
field from the resource itself would be.

**Non-goals**

- Not a multi-tenancy architecture requirement. Shared or separate stores both
  satisfy this.
- Not a check of isolation for audit records, background jobs, caches, or
  non-FHIR surfaces.
- Not an enumeration-resistance claim. The suite does not attempt id guessing,
  search-based discovery, or timing analysis.

**What 0.1.0 verifies** — `probe_tenant_isolation`, 2 checks: `resource is not
readable from another tenant`, `resource IS readable from its own tenant`.

**Divergences from the requirement**

| Divergence | Consequence |
|---|---|
| The cross-tenant check passes when `status != 200` **or** the returned id differs. | 403 passes. A 200 returning a *different* resource passes. Requirement 3 is not enforced by the suite. |
| One second-tenant id, one read-by-id. | Requirement 4 (search and aggregate paths) is **[A]**. |
| The cross-tenant caller in the probe presents a tenant header only. | This is the read-auth gap again (§4): on a deployment with read authentication off, the property is measuring path filtering rather than authorization. |

---

### P6 — Medical disclaimer

**Normative requirement**

1. A response carrying clinical data MUST carry a machine-readable notice that
   the data is not clinical advice. **[H]**
2. The notice MUST be attached to the payload it disclaims, not returned in
   place of it. **[H]**

**Say plainly what this property is.** It is a labelling requirement, not a
security control. No adversary is defeated by it and no data is protected by it.
It occupies one seventh of the grade, the same weight as tenant isolation. That
is a defect in the grading function rather than in the property, and it is
recorded as **G-D** in §7. A future version should either reweight the grade or
move this out of the guardrail set.

**How it is observed from outside**

```
POST /r6/fhir/Observation  (confirmed clinical write)  -> 201, id
GET  /r6/fhir/Observation/{id}                         -> 200, IS the
     Observation, and carries a disclaimer alongside the clinical content
```

**What a conforming refusal looks like**

Not applicable. An error response that happens to contain the word "disclaimer"
MUST NOT satisfy this property — which is why requirement 2 exists and is
separately probed.

**Non-goals**

- Not a claim about the text's legal sufficiency.
- Not a claim that any user interface displays it.
- Not per-entry coverage inside a Bundle.

**What 0.1.0 verifies** — `probe_medical_disclaimer`, 2 checks: `clinical read
carries a medical disclaimer`, `the disclaimer accompanies the clinical record,
not an error`.

**Divergences**

| Divergence | Consequence |
|---|---|
| Requirement 1's check is a substring test over the serialized body. | Any occurrence of the word satisfies it; the second check is what stops that from being vacuous. |
| Bundle-level disclaimers are added when any entry is clinical. | A mixed Bundle carries one notice, not one per entry. Not probed. |

---

### P7 — Error fidelity

The property that fails through the proxy, and the reason HealthClaw's honest
answer is Grade B rather than Grade A.

**Normative requirement**

1. A refused request MUST answer with a corrective `OperationOutcome` that names
   what was wrong in terms the caller can act on. **[H]** (local profile)
2. A refusal MUST NOT echo caller-supplied or upstream-supplied free text, URLs,
   or internal identifiers. **[H]**
3. A refusal MUST be audited as a failure, distinguishably from a success.
   **[H]**
4. An implementation MUST NOT silently ignore a parameter it did not honour. In
   lenient handling it MUST carry a search-mode outcome warning naming the
   ignored parameter, the self link MUST omit that parameter, and the audit
   record MUST say it was ignored rather than applied. **[H]**
5. A refusal generated upstream MUST be translated, not forwarded. The caller
   holds no relationship with the upstream and cannot repair its errors. **[A]**
   — this is [#498](https://github.com/aks129/HealthClawGuardrails/issues/498), and it is the failing property in proxy
   mode.
6. An authorization failure MUST NOT be reported as not-found, and a timeout
   MUST NOT be reported as a successful empty result. **[H]** (proxy profile,
   when run)

**How it is observed from outside**

```
GET /r6/fhir/Observation?patient={ref}&datetime=x
    Prefer: handling=strict   -> 400 + OperationOutcome naming `datetime`
                                 and the supported parameter set
    (no Prefer)               -> 200 + Bundle whose self link omits
                                 `datetime` and which carries a search-mode
                                 outcome warning naming it
GET /r6/fhir/Observation?patient={ref}&code:exact=x
                              -> 400 + OperationOutcome naming the
                                 unsupported modifier
GET /r6/fhir/AuditEvent?...   before and after each, to confirm the
                                 rejections were audited as failures
```

Every probe search is bound to a synthetic subject. An error-fidelity probe that
searched unbounded would read real data to test an error path.

**What a conforming refusal looks like**

An `OperationOutcome` with **only** `resourceType` and `issue`; each issue
carrying **only** `severity`, `code`, and `details.text`; severity `error` or
`fatal`; code in {`invalid`, `structure`, `value`, `not-supported`}; text
non-empty and free of URLs and of any value the caller supplied.

The shape is deliberately narrow. `diagnostics` is excluded because it is where
implementations put stack traces and upstream messages, and a leak there is
indistinguishable from a helpful error.

**Non-goals**

- Not the write path. This property grades read and search refusals.
- Not the action rail, the SHL surface, or MCP exception messages
  ([#153](https://github.com/aks129/HealthClawGuardrails/issues/153)).
- Not a claim about any profile marked `not_run`.

**What 0.1.0 verifies** — `probe_error_fidelity`, local profile, 6 checks:
`strict unknown parameter is rejected`, `strict rejection is audited as a
failure`, `lenient unknown parameter carries an outcome warning`, `lenient
warning is audited truthfully`, `unsupported modifier is rejected`, `unsupported
modifier rejections are audited as failures`. Optional `mcp` and `proxy` profiles
add checks when configured. The property grade is the **worst executed profile**.

**Divergences from the requirement — including the one that blocks third-party
conformance**

| Divergence | Consequence |
|---|---|
| **The local A contract requires the implementation's declared supported-parameter set to equal HealthClaw's exact eight** — `patient, code, status, _lastUpdated, _count, _sort, _summary, context-id` — including the HealthClaw-specific `context-id`. Executed evidence in §7, **G-A**. | **Fixed in #525.** This row records what `0.1.0-draft` shipped with: a third-party server refusing `datetime` just as correctively but supporting a different parameter set capped at C, which capped the property, which capped the deployment at Grade B. The requirement is now shape-based and names no parameter of ours. See §7 G-A for the second mechanism that had to move with it. |
| Requirement 5 is **[A]** and known-false in proxy mode. | HealthClaw scores B through the proxy. Cited from [#498](https://github.com/aks129/HealthClawGuardrails/issues/498); **not measured by the author of this document**. |
| `proxy` and `mcp` default to `not_run`. | A local Grade A for this property is measured over one profile and says nothing about a deployment with an upstream configured. The report states `coverage=local-fhir-only`; a reader who stops at "A" will over-read it. |

---

## 3. Requirements on the conformance report itself

A report is an artifact that gets forwarded, quoted, and pasted into a partner's
review. These requirements exist because each of them has been violated here.

| | Requirement | Status |
|---|---|---|
| **R1** | A report MUST state per-property profile coverage and MUST NOT list an unexecuted profile as run. | **[H]** `test_report_exposes_property_grade_and_profile_coverage` |
| **R2** | A published grade below A MUST name the failing property. | **[A]** — stated policy; nothing enforces it in the artifact |
| **R3** | A report MUST publish the list of guardrails it does **not** grade, beside the ones it does. | **[H]** `tests/test_ungraded_is_published.py` |
| **R4** | A report MUST NOT print a failure-explanation string beside a passing check. | **[H]** the `observed` / `on_failure` split in `Check`; suppressed in `to_dict` |
| **R5** | A report MUST cite the specification version it was measured against. | **[A]** — nothing emits it today. This document is version 0.1.0-draft; no existing report names it |
| **R6** | A report MUST state that the probes write and do not clean up, and name the tenant they wrote to. | **[I]** the runner warns; the report carries the tenant |

R4 is not a formatting preference. Fifteen checks shipped emitting
`{"name": "no raw SSN in the audit trail", "passed": true, "detail": "PHI leaked
into audit"}`. Two readers independently concluded the deployment was leaking
PHI. The second was a physician advisor, about thirty hours before a demo
recording.

---

## 4. Explicitly not graded

Published beside the grade, because a reader who sees "7/7, 35 checks" and no
exclusions will assume the seven are the whole surface. Source:
the `UNGRADED` tuple in `r6/conformance/probes.py`.

| Not graded | Why it matters |
|---|---|
| **Read authentication** | Whether an unauthenticated caller can read another tenant's records. `READ_AUTH_ENABLED` defaults off and is off in the harness fixture, so a deployment serving records to strangers scores what a gated one scores ([#401](https://github.com/aks129/HealthClawGuardrails/issues/401)). This is the first thing a skeptical adopter checks and the suite is silent on it. |
| **The action rail's separation** | That propose, commit and confirm cannot be collapsed into one step. |
| **Step-up refusals beyond a forged token** | Expired, cross-tenant, read-scope-on-write and replayed-nonce are each verified by unit tests, none by this suite. |
| **Redaction on `$lastn` and `SubscriptionTopic/$list`** | Graded on reads and searches, not on these operations. |
| **Rate limiting, the mint gate, and purge** | No probe exercises any of them. The grade says nothing about whether a caller can flood the API, mint a token it should not have, or leave data behind after a delete. |

#401 is instructive about why this list exists rather than being closed by adding
probes. A read-auth probe that passes when read auth is off would reproduce the
very defect: the suite cannot distinguish "off because this is a public demo
tenant" from "off because somebody disabled it". Grading it needs a
**declared-posture manifest** — the deployment states its intended posture and
the suite grades against the declaration — which is design work, not a probe.
#401 was closed by publishing the exclusion (PR #482), not by hiding it.

---

## 5. Threat model

Four adversaries. For each: what the guardrails stop, and what they do not.

### 5.1 A1 — An agent with a valid credential, doing more than the user meant

The primary adversary. Not malicious by assumption: a capable model, a broad
prompt, and a token that works.

**Stopped**

- It cannot mint its own write credential (P3), and a tampered one is refused.
- It cannot read another tenant's records by id (P5).
- It cannot act without leaving a retrievable record (P2).
- What it does read comes back reduced: initials, masked identifiers, no
  telecom, no street address, no narrative (P1). Bulk exfiltration through the
  graded surface yields a degraded copy.
- Labels it sees come from the implementation's terminology table, so it cannot
  be steered by text an upstream wrote (P1.4).

**Not stopped**

- **It self-confirms clinical writes.** `X-Human-Confirmed` is a header the
  agent sets ([#214](https://github.com/aks129/HealthClawGuardrails/issues/214)). The 428 is a speed bump for an agent
  that has not read the error message, and the error message says how to pass
  it.
- **The graded surface is not the whole read surface.** Several read paths never
  call redaction ([#282](https://github.com/aks129/HealthClawGuardrails/issues/282), [#382](https://github.com/aks129/HealthClawGuardrails/issues/382)). An agent
  that finds them gets unreduced records with a Grade A badge on the wall.
- **Nothing bounds volume.** Rate limiting is ungraded (§4); the suite cannot
  distinguish one read from a hundred thousand.
- **It can write caller-controlled text into the audit trail** through an
  unvalidated resource id ([#279](https://github.com/aks129/HealthClawGuardrails/issues/279)). Audit `detail` stays
  PHI-free; PHI walks in through the adjacent column.
- **Token expiry, scope, and replay refusals are ungraded** (§4).
- On a deployment with the consumer upload path, a revoked connection still
  accepts uploads ([#280](https://github.com/aks129/HealthClawGuardrails/issues/280)).

### 5.2 A2 — A caller probing for another tenant's data

**Stopped**

- Read-by-id across the tenant boundary, with a positive control proving the
  record exists and is served to its owner (P5).
- The refusal is 404 in HealthClaw, so it does not confirm existence.

**Not stopped**

- **Read authentication is ungraded and defaults off** ([#401](https://github.com/aks129/HealthClawGuardrails/issues/401)).
  Against a deployment that never asks who the caller is, this property degrades
  from authorization to path filtering, and the grade does not change.
- Search-based and aggregate-path enumeration is unprobed (P5, requirement 4,
  **[A]**).
- Existence non-disclosure is a SHOULD the suite does not check: 403 passes the
  probe.
- Timing, error-shape, and id-structure side channels are out of scope
  entirely.

### 5.3 A3 — An operator misconfiguring the deployment

The adversary with the best access and no hostile intent, which is why this is
the section most likely to be true of a real incident.

**Stopped**

- The suite grades the deployment **as configured**, so a redactor that stopped
  running or a step-up gate that stopped validating is detected — that is what
  the mutation tests in `tests/test_guardrail_conformance.py` exist to keep
  true.

**Not stopped**

- **`READ_AUTH_ENABLED` off in production.** Ungraded ([#401](https://github.com/aks129/HealthClawGuardrails/issues/401)).
- **Single-use nonce falls back to a per-process dict without `REDIS_URL`**
  ([#212](https://github.com/aks129/HealthClawGuardrails/issues/212)). On multi-worker gunicorn a captured confirm token
  replays on a sibling worker within its TTL. The deployment fails closed on
  Redis *errors* and open on Redis *absence*. No probe sees it.
- **Path-prefix exemptions.** `enforce_human_in_loop` exempts any path
  containing `/demo/` or `/internal/`
  (`r6/health_compliance.py`). The suite never probes those prefixes. This
  exact class was live in production once: an unauthenticated cross-tenant write
  on a `/demo/` route ([#210](https://github.com/aks129/HealthClawGuardrails/issues/210), closed).
- **A write credential rendered into a browser page and served cross-origin**
  ([#395](https://github.com/aks129/HealthClawGuardrails/issues/395)) — a documented invariant said the browser never
  sees tenant-bound step-up tokens, and for one route it did.
- **Startup misconfiguration.** A typo in an upstream-kind variable started the
  app and 500'd every request ([#518](https://github.com/aks129/HealthClawGuardrails/issues/518), fixed). A conformance
  run would have caught it; nothing ran one.

### 5.4 A4 — An upstream server that behaves differently from ours

Every connector is a different FHIR server with its own error vocabulary, its
own referential-integrity rules, and its own free text.

**Stopped, in principle**

- Upstream `display` and `CodeableConcept.text` are stripped before the response
  is assembled, and labels are re-derived from codes (P1.3, P1.4). This is the
  designed defense against an upstream that writes patient names into free-text
  fields — a real leak class here, not a hypothetical
  ([#207](https://github.com/aks129/HealthClawGuardrails/issues/207)/[#209](https://github.com/aks129/HealthClawGuardrails/issues/209)).
- Upstream error text must not reach the caller (P7.2).

**Not stopped**

- **Error fidelity degrades in proxy mode** ([#498](https://github.com/aks129/HealthClawGuardrails/issues/498)). Unknown
  search parameters and unsupported modifiers are forwarded upstream rather than
  refused, and the caller receives the upstream's 404 or 502. An agent cannot
  self-correct from a 502.
- **The proxy profile defaults to `not_run`.** A local Grade A is measured over
  a profile that has no upstream in it.
- **A create echoes the caller's upstream `display` back unredacted**
  ([#380](https://github.com/aks129/HealthClawGuardrails/issues/380)).
- **Upstream referential integrity changes what a probe means.** A probe whose
  subject the upstream rejected can score a pass on the failure of a guardrail
  that was working. That happened: a constant Patient body, a server that
  refuses duplicates, and a scorecard reading **Grade F, 1/7, with four of six
  failures blaming the guardrails**. The synthetic subject is now unique per run
  and clinical probes bind to a real subject where one can be created. Any
  implementation of this suite MUST do the same.
- **MCP error paths beyond the read path forward raw backend text**
  ([#153](https://github.com/aks129/HealthClawGuardrails/issues/153)).

### 5.5 A note on the report as an attack surface

Not an adversary, but the failure mode with the worst record here. Five of this
project's most polished artifacts made claims the code did not support, and in
every case the polish is what suppressed inspection — a rough edge invites
inspection, a confident sentence next to a passing test does not. A conformance
report is the most
forwardable artifact this system produces. §3's requirements — coverage
declared, failing property named, exclusions published, no failure text beside a
pass, version cited — exist to make the report unable to say something the run
did not establish.

---

## 6. What these guardrails do NOT stop

The three sharpest first, then the rest. This section is the one a partner
should read before the property list.

### 6.1 An agent with a write token approves its own clinical writes

`X-Human-Confirmed: true` is set by the caller. The 428 refusal names the header
it wants. An agent that reads the error message satisfies the human gate on its
next request, with no human anywhere in the loop.
[#214](https://github.com/aks129/HealthClawGuardrails/issues/214).

What is true: the gate discriminates, and the action rail's separate approval
endpoint is a real out-of-band mechanism. What is not true is the sentence "and
clinical writes need a human" applied to direct FHIR writes.

### 6.2 Grade A covers one route family, and the data leaves through others

The suite probes `/r6/fhir`. Read paths that never call `apply_redaction` are
enumerated in [#282](https://github.com/aks129/HealthClawGuardrails/issues/282) — labs, care gaps, quality, actions,
form-fill, SDC documents, SMBP — and [#382](https://github.com/aks129/HealthClawGuardrails/issues/382) adds
`r6/brief/`, which was on neither probe list and which reads
`CodeableConcept.text` and `coding[].display` into a document a patient and
their clinic receive.

An implementer reading "reads come back redacted" should read it as: **reads
through the graded surface come back redacted.** Any route family added to a
deployment is outside this grade until a probe covers it.

### 6.3 A deployment that serves records to anonymous callers scores the same as one that does not

`READ_AUTH_ENABLED` defaults off. No property grades it. This is disclosed
rather than fixed, deliberately, and the reasoning is in §4 —
[#401](https://github.com/aks129/HealthClawGuardrails/issues/401).

### 6.4 Redaction truncates identifiers; it does not remove them

The HealthClaw profile keeps the last four characters of every identifier value
(`***6789`), retains `identifier[].system`, and truncates birthDate to a year.
That is Safe-Harbor-*style* field redaction and it is **not a legal
de-identification determination**: the Privacy Rule's Safe Harbor method
requires the Social Security number removed, not shortened, and a last-four SSN
is a recognised re-identification vector. `SECURITY.md` was corrected to say so
on 2026-08-16 ([#511](https://github.com/aks129/HealthClawGuardrails/issues/511)); the roadmap item is
[#112](https://github.com/aks129/HealthClawGuardrails/issues/112).

The behaviour is defensible as a compensating control. The unhedged claim is
not, and this specification therefore treats the transformation as
implementation detail (P1) rather than freezing it as a requirement.

### 6.5 Grade B is one property of seven, and the seven are not equal

6/7 is B whether the missing property is error fidelity or tenant isolation.
Read a sub-A grade only together with the name of the failing property (§3, R2).

### 6.6 The suite does not test immutability, rate limits, expiry, replay, purge, or the action rail

See §4. Each is verified by internal unit tests or not at all, and neither of
those is what a conformance grade means.

---

## 7. Known gaps register

Issues first, then observations recorded while writing this document. Nothing in
the second group was fixed; recording them is the deliverable, per the process
this document sits inside.

### 7.1 Tracked

| Issue | Shape | Where it lands in this spec |
|---|---|---|
| [#214](https://github.com/aks129/HealthClawGuardrails/issues/214) | `X-Human-Confirmed` is a client-set header | P4 requirements 1–2 **[A]**; §6.1 |
| [#282](https://github.com/aks129/HealthClawGuardrails/issues/282) | eight read paths never call `apply_redaction` | §6.2 |
| [#382](https://github.com/aks129/HealthClawGuardrails/issues/382) | `r6/brief/` reads unredacted, on no probe list | §6.2 |
| [#401](https://github.com/aks129/HealthClawGuardrails/issues/401) | read auth ungraded; defaults off | §4, §6.3 |
| [#498](https://github.com/aks129/HealthClawGuardrails/issues/498) | error fidelity degrades in proxy mode | P7 requirement 5 **[A]**; the named Grade B property |
| [#112](https://github.com/aks129/HealthClawGuardrails/issues/112) | de-identification rigor; claim boundary | P1 non-goals; §6.4 |
| [#212](https://github.com/aks129/HealthClawGuardrails/issues/212) | nonce falls back to a per-process dict | §5.3 |
| [#279](https://github.com/aks129/HealthClawGuardrails/issues/279) | audit `resource_id` is caller-controlled | P2 divergences; §5.1 |
| [#280](https://github.com/aks129/HealthClawGuardrails/issues/280) | a revoked connection still accepts uploads | §5.1 |
| [#321](https://github.com/aks129/HealthClawGuardrails/issues/321) | `install_audit_assertions` false-positive class | P2 divergences |
| [#367](https://github.com/aks129/HealthClawGuardrails/issues/367) | `form_fill._subject_label` prefers `name[0].text` | P1 requirement 3 |
| [#380](https://github.com/aks129/HealthClawGuardrails/issues/380) | a create echoes the caller's display back | P1 non-goals; §5.4 |
| [#395](https://github.com/aks129/HealthClawGuardrails/issues/395) | step-up token rendered into the browser | P3 requirement 5 **[A]**; §5.3 |
| [#153](https://github.com/aks129/HealthClawGuardrails/issues/153) | MCP sanitization beyond the read path | P7 non-goals; §5.4 |

### 7.2 Observed while writing this specification — no issue filed

Recorded with evidence rather than fixed. Each is a candidate issue for the
owner, not a diff.

**G-A — the local error-fidelity contract is not portable to a third party.**
`_outcome_names_parameter_and_supported_set` requires set equality between the
implementation's declared supported parameters and HealthClaw's own eight,
including the HealthClaw-specific `context-id`. Executed 2026-08-16 against
`r6/conformance/probes.py` at `4cb3771`, read-only:

```
required set: ['_count', '_lastupdated', '_sort', '_summary', 'code',
               'context-id', 'patient', 'status']

healthclaw                         corrective=True  grade=A
third-party (standard params)      corrective=False grade=C
healthclaw minus context-id        corrective=False grade=C
```

The second row is a server whose refusal is equally corrective and whose
supported set is `patient, code, status, category, date, _lastUpdated, _count,
_sort, _summary, _include`. It graded C, which capped `error_fidelity`, which
capped the deployment at Grade B.

**FIXED, #525.** The normative requirement is now what §3 P7 always said it
was: the refusal **names the offending parameter and declares a non-empty,
well-formed supported-parameter set that does not contain it.** Which
parameters those are is the implementing server's business. No comparison to
our set remains anywhere in the suite.

**A second mechanism was found during the fix, and it is the reason a
one-line change would not have worked.** `_outcome_has_unsafe_last_updated_
suggestion` strips the declared-set sentence before scanning for
`_lastUpdated` — because naming it among what you support is a fact, not a
suggestion to use it in place of a clinical date. That strip *also* applied
only when the set was exactly ours. `_lastUpdated` is one of ours, so the
strip existed purely to let our own declaration through: every other server
that truthfully listed `_lastUpdated` tripped the heuristic on its own
legitimate sentence and was forced to C by a **second, independent path**.
Relaxing set equality alone would have looked complete and changed nothing.

Both moved together, each pinned by a two-way mutation
(`tests/test_guardrail_conformance.py`). Restoring either one alone turns the
tests red, which is what makes them independent rather than redundant.

**What the fix deliberately does not do.** It cannot tell an unusual-but-real
parameter from a plausible fake — `patiently, barcode` now grades as a
declaration. That is accepted rather than overlooked: `context-id` looks
invented from anyone else's side too, and the alternative is a whitelist of
"real" FHIR parameters, which recreates this defect with more steps. A
US Core-anchored family requirement is a candidate `0.2.0` tightening. The
anti-vacuity checks that do not depend on resemblance all survive, including
the one that matters most — `_lastUpdated` offered as a substitute for a
clinical date is still refused.

Severity was high for the standard-setter thesis, none for our own
deployment. Our own grade is unchanged: A, 7/7.

**G-B — "Immutable Audit Trail" is not tested for immutability.** No probe in
`r6/conformance/probes.py` attempts to modify or delete an AuditEvent. The word
sits in the property name printed on every passing scorecard. Either the probe
or the name should change. Severity: medium — it is a claim in a forwardable
artifact.

**G-C — this document is a public surface the de-identification language guard
does not open.** `tests/test_deidentification_language.py` scans a fixed list
plus `docs/blog/*.md` and `docs/recipes/*.md`. `docs/specs/*.md` is in neither.
This file names the Safe-Harbor standard and hedges it by hand. Its text was
checked against the guard's rule and passes; it is simply not one of the files
the guard opens, so nothing enforces that the next revision keeps passing, and
nothing would catch the next spec that does not.

This is the recurring shape in this project's defect history — a guard written
from the fix in front of it rather than from the property, which then certifies
the gap. The guard's own docstring records the last instance: it matched four
exact phrases across nine files, and the unhedged claim was sitting in
`SECURITY.md`, which was not one of them. The fix here is one glob, and it
belongs to whoever owns that guard, not to this document.

**G-D — the grade is a flat fraction over seven unequal properties.** §1.4 and
§6.5. A future version should either weight the properties or state a floor
(for example: no deployment failing tenant isolation may be published above F).

---

## 8. Running this yourself, and what a portable suite would take

**How we prove it works, with what data, run by whom** — the architecture
review's fourth question, answered concretely because for a specification the
answer *is* a suite an outsider can run.

### 8.1 What exists today

```
# Against a running deployment, from a clone of this repository
python scripts/guardrail_conformance.py \
    --base-url https://<host> \
    --step-up-token <minted via POST /r6/fhir/internal/step-up-token>
    [--tenant <self-test tenant>] [--mcp-url <streamable http endpoint>] [--json]

# Or from the deployment itself
GET /r6/fhir/$conformance             # JSON scorecard, 200 at A, 503 otherwise
GET /r6/fhir/$conformance?format=text # human-readable
GET /r6/fhir/$conformance?format=shields
```

The probes write synthetic data to the tenant they grade and do not clean up.
`--tenant` defaults to a dedicated self-test tenant for that reason.

**Data:** synthetic only, generated by the probes. No real record is required or
permitted. A conformance run needs no BAA, no clearance, and no healthcare
background — which is what makes it delegable.

**Run by:** the integrator, on their own deployment. That is the whole point of a
conformance suite, and it is also the reward: the scorecard is theirs to publish.

### 8.2 What it would take to be genuinely third-party runnable

Honestly, today it is not. Six things stand between the current harness and a
suite a vendor could run against a non-HealthClaw implementation:

1. **Decouple the supported-parameter set** (G-A). This is the blocker. As long
   as Grade A requires an implementation to declare exactly our eight
   parameters, "run this against your server" is an invitation to score B.
2. **Make the route prefix configurable.** `/r6/fhir` is the default in both
   probe clients and is threaded through every probe path.
3. **Accept the full range the specification allows** — 403 as well as 401 on
   P3, 404 or 403 on P5 — instead of the single status HealthClaw returns.
4. **Ship the runner without the implementation.** `scripts/guardrail_conformance.py`
   inserts the repo root on `sys.path` and imports `r6.conformance`. A third
   party must clone our application to grade their own.
5. **A declared-posture manifest**, so read authentication becomes gradable
   (§4) rather than permanently excluded.
6. **A version-stamped report** (§3, R5) and a published expected-scorecard
   fixture, so two runs can be compared and a claim can be checked.

Items 1 and 4 are the ones that decide whether #234 produces a standard or a
document. Neither is large; both are outside this task's scope, which was to
write the specification and not to change the code.

---

## 9. The architecture review's four questions

**Does this serve the vision, or is it adjacent work that feels productive?**
It serves it, with one real risk. The thesis is that guardrail claims should be
*verifiable*, and a versioned normative document is what turns a project into
something a second implementer can build to. The risk is that this becomes one
more polished artifact making claims the code does not support — the failure
mode described in §5.5, and the one with the worst record here. The mitigation
is structural: every normative
statement carries **[H]**, **[I]** or **[A]**, and §2's divergence tables are
written against the test file rather than against the intent.

**What is the honest failure mode, and who notices it first?**
A partner implements to this document, runs the suite against their server, and
discovers within an hour that they cannot score A on error fidelity because
their search parameters are not ours (G-A). That is the most likely first
external experience of this artifact today, and it would be a worse first
impression than having published nothing. The second failure mode is slower and
worse: a partner reads "reads come back redacted", deploys a route family the
suite does not probe, and the grade on the wall covers a surface the data does
not leave through (§6.2).

**What does it make harder later?**
Publishing property definitions makes changing them a versioned event rather
than a commit. That is the intended cost. Two specific traps: the redaction
transformation must stay **implementation detail** (P1), because last-four
identifier truncation is a rule we should want to change and freezing it would
make the improvement a breaking change; and a published grade invites
conformance claims from deployments we do not control, which needs an answer
before 1.0.0 — probably that a self-run scorecard is a self-assessment and may
not be described as certification.

**How will we prove it works, with what data, run by whom?**
§8. A conformance suite an outsider can run, on synthetic data, by the
integrator, with the scorecard as the reward. The honest state is that the suite
exists, we run it, and it is not yet portable — six named items, one of them the
blocker.

---

## 10. Versioning

`MAJOR.MINOR.PATCH`, with pre-1.0 semantics.

| Bump | Means |
|---|---|
| **PATCH** | Editorial. No change to what conforms. A deployment conforming to `0.1.0` conforms to `0.1.1`. |
| **MINOR** | A property is added, a SHOULD becomes a MUST, or a divergence is closed. A deployment conforming to `0.1.x` MAY NOT conform to `0.2.0`. Every published grade MUST name the version it was measured against (§3, R5). |
| **MAJOR** | The property set changes shape, or a requirement is removed. |
| **`-draft`** | Nothing is stable. Any part of this document may change without a version bump until `0.1.0` is released. |

**1.0.0 means two specific things**, not "we are happy with it": the seven
properties are stable, and the suite is runnable by a third party against a
non-HealthClaw implementation (§8.2). Until item 1 of §8.2 is closed, this
document cannot honestly go past 0.x.

A deployment's grade is meaningless without a version. "Grade A" is not a claim;
"Grade A against guardrail-spec 0.1.0-draft, local profile, coverage
local-fhir-only" is.

---

## 11. Credits

Contributors who find a defect in these properties, or in the suite that grades
them, are named here with their finding. That is the intended reward for
external testers and integrators, and it costs us nothing we should want to
keep.

*No external contributors yet. This section is the mechanism, published before
there is anyone in it, so it is not a promise made after the fact.*

Report a finding through `SECURITY.md` for anything exploitable, or as an issue
otherwise. A finding that a check **passed without its subject ever running** is
worth more to us than a feature, and will be credited as such.

---

## Appendix A — property, probe, and check map

Traceability from every claim in §2 to the code that verifies it. Sources:
`r6/conformance/probes.py`, `tests/test_guardrail_conformance.py` at `4cb3771`.

| Property | Probe | Checks | Anti-vacuity test |
|---|---|---|---|
| P1 PHI redaction | `probe_phi_redaction` | 17 | `test_a_search_that_returns_nothing_no_longer_scores_a` |
| P2 Audit trail | `probe_audit_trail` | 3 | (read discriminator is inline; no mutation test) |
| P3 Step-up | `probe_step_up_enforcement` | 3 | `test_step_up_validation_turned_off_no_longer_scores_a` |
| P4 Human confirmation | `probe_human_in_the_loop` | 2 + note | `test_a_gate_that_blocks_confirmed_writes_too_is_not_a_gate` |
| P5 Tenant isolation | `probe_tenant_isolation` | 2 | `test_a_deployment_that_refuses_everyone_no_longer_isolates` |
| P6 Medical disclaimer | `probe_medical_disclaimer` | 2 | `test_the_word_disclaimer_in_an_error_is_not_a_disclaimer` |
| P7 Error fidelity | `probe_error_fidelity` | 6 local, +1 mcp, +5 proxy | `test_local_error_probes_always_bound_searches_to_a_synthetic_subject` |
| whole report | `run_conformance` | grade | `test_a_deployment_that_answers_nothing_scores_f` |

The anti-vacuity column is the column that matters. Every check in this harness
except two used to be an **absence** assertion, and a broken guardrail passes
those *harder* — it shrinks the response. The tests in that column turn one
guardrail off at a time and require the grade to follow.

## Appendix B — normative versus implementation detail, at a glance

What another implementer must match, and what they may decide for themselves.

| Normative (MUST match) | Implementation detail (implementer's choice) |
|---|---|
| Reads do not return direct identifiers in the form the record holds them | Whether they are removed, masked, or truncated, and to what |
| Labels are derived from codes by the implementation, after stripping upstream text | Which terminology table, and which codes are covered |
| The response is the resource requested, not an empty or error body | Bundle shape, paging |
| Every access emits a retrievable audit record; a read is distinguishable from a write | `AuditEvent` at `/AuditEvent`, `action=R`, the search shape |
| Audit detail carries no PHI | Storage, retention, export format |
| Writes require a credential the implementation issued and can verify, and the gate discriminates | Token format, header name, HMAC vs signed vs opaque |
| A write refusal is 401 or 403, never 2xx | Which of the two (the 0.1.0 suite requires 401 — a divergence, §2 P3) |
| A clinical write requires a confirmation the agent cannot produce alone | The mechanism (HealthClaw's header does **not** satisfy this) |
| An unconfirmed clinical write is refused with 428, and a confirmed one accepted | Which resource types are clinical |
| Cross-tenant read returns nothing of the resource; the owning tenant still gets it | 404 vs 403 (404 preferred), tenant header name, store topology |
| Clinical responses carry a machine-readable notice attached to the payload | The wording, the field name, the URL |
| Refusals are corrective, echo no supplied free text, and are audited as failures | The exact `OperationOutcome` codes, within the stated sets |
| Ignored parameters are declared, not silently dropped | Strict/lenient signalling mechanism |
| The report declares coverage, names failing properties, publishes exclusions, cites a version | Rendering, transport, badge format |
