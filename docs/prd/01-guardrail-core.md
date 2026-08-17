# PRD 1 — Guardrail core

> Owner brief: `.claude/agents/owner-guardrail-core.md` · Process:
> `docs/2026-08-16-delivery-process.md` · Topology:
> `docs/2026-08-16-system-topology.md`
>
> Measured 2026-08-16. A line that says *unmeasured* means nobody has run it,
> not that it is broken.

## 1. The problem, and whose it is

An agent given a FHIR credential can read every record, write anything, and leave no trace. Nothing in FHIR itself stops it. The user here is **the integrator putting an agent in front of a record system** — and behind them, the patient whose record it is.

The problem is not that agents are dangerous. It is that today the only available answers are *give it full access* or *give it none*, and neither is usable.

## 2. What "works" means

> Reads come back redacted, every access is audited, writes need a credential the agent cannot mint, clinical writes need a human, and one tenant cannot see another's data — demonstrably, on a running system.

Conformance **Grade A (7/7)** locally and **Grade B (6/7)** through the proxy, *with the failing property named*. Grade B is an acceptable answer. Grade B reported as Grade A is not, and neither is Grade A bought by weakening a probe.

## 3. How it is proven

- **Run log** — `tests/test_guardrail_conformance.py` locally, plus the live `$conformance` endpoint through a real FHIR server.
- **Recording** — the write-gate matrix (428 / 401 / 428 / 201) and a redaction read, driven by the run that asserts them.
- **Register** — every property the harness scores as passed, checked that its *subject actually ran*.
- **Sign-offs** — QA adversarial; end-user is a partner integrator, not us.

## 4. Current state, measured

- **Grade A, 7/7, 35 checks** — measured today, twice.
- **Proxy mode: not measured.** The pack does not report a grade it did not take. Blocked on the local stack.
- The failing property in proxy mode is `error_fidelity` (#498); the local Grade A for it carries `coverage=local-fhir-only`, so the A and the proxy failure are measured over different profiles and do not contradict.
- Pack: `docs/evidence/2026-08-16-set1-guardrail-core.md` — **EVIDENCE PARTIAL**.
- Honest summary from that pack: *verified against the application code, unverified against the deployment*.

## 5. Known gaps — the open issues in this set

| # | Issue | Shape |
|---|---|---|
| 214 | `X-Human-Confirmed` is the entire human gate on direct clinical writes | known gap, client-supplied header |
| 282 | several read paths never call `apply_redaction` | coverage hole |
| 382 | `r6/brief/` reads unredacted and is on no probe list | coverage hole |
| 380 | a CREATE echoes the caller's upstream display back unredacted; a read does not | asymmetry |
| 395 | the step-up token is rendered into the patient's browser, cross-origin | credential exposure |
| 212 | single-use nonce falls back to a per-process dict without `REDIS_URL` | silent degradation |
| 498 | error fidelity degrades in proxy mode — unknown search params forwarded, not refused | the Grade B property |
| 321 | `install_audit_assertions` has a false-positive class | guard reliability |
| 334 | the access kernel strips the step-up token, widening 6 sites during slices 4–8 | owner decision |
| 112 | de-identification rigor (Expert Determination) and profile validation | roadmap, claim boundary |
| 234 | ship a versioned guardrail spec + written threat model | standard-setter |
| 168 | FTC Health Breach Notification Rule likely applies | compliance decision |
| 279 | audit trail can carry caller-controlled text via unvalidated resource ids | injection |
| 280 | a revoked connection can still accept uploads | authorization |
| 408 | `/internal/ingest-bundle` echoes the caller's raw resourceType | echo |
| 367 | `form_fill._subject_label` prefers `name[0].text` | the display-leak family |
| 509 | two soft-delete defects pointing the opposite way to #422 | one half fixed, one is a product ruling |
| 153 | extend error-fidelity sanitization beyond the read path | coverage |
| 281 | `R6Resource.id` is String(255) with no length check | SQLite/Postgres divergence |
| 286 | a blank resource id is refused rather than auto-assigned | decide which is correct |
| 287 | the id-validation tests only work as an ensemble | do not delete individually |

## 6. Specifications

- `docs/2026-08-03-access-kernel-spec.md` — the interface contract. §1.2b covers `has_grant`; §2.5b the remaining slices.
- `docs/2026-08-03-audit-assertion-ruling.md`
- `docs/development.md` § Security invariants
- **Missing, and a SOW item:** a versioned, published guardrail specification with a threat model (#234). Today the contract is seven properties in a test file.
